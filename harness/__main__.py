"""uv run python -m harness <command>. See CONTRIBUTING.md."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib

import jsonschema

from harness import fakeapi, i18n, matrix, scan, snippets
from harness.snippets import ROOT, load_meta, use_cases

CLIENTS = {
    "python": ["clients/python/stack_client.py", "clients/python/coco_rle.py"],
    "typescript": ["clients/typescript/stackClient.ts"],
    "go": ["clients/go/stackclient/client.go", "clients/go/stackclient/rle.go"],
}


def copies():
    """(canonical file, copy) for every client file found in an implementation directory."""
    for lang, files in CLIENTS.items():
        for f in files:
            src = ROOT / f
            if not src.exists():
                continue
            sub = "stackclient/" if lang == "go" else ""
            for impl in sorted(ROOT.glob(f"use-cases/*/{lang}")):
                dst = impl / (sub + src.name)
                if dst.exists() or (lang == "python" and src.name == "stack_client.py") or (lang != "python" and src.name in ("stackClient.ts", "client.go")):
                    yield src, dst


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check_contract():
    problems = []
    for f in sorted((ROOT / "contract").glob("*.json")):
        try:
            jsonschema.Draft202012Validator.check_schema(json.loads(f.read_text()))
        except (ValueError, jsonschema.SchemaError) as exc:
            problems.append(f"{f.name}: {exc}")
    uc_schema = jsonschema.Draft202012Validator(matrix.contract("usecase"))
    man_schema = jsonschema.Draft202012Validator(matrix.contract("manifest"))
    for uc in use_cases():
        meta = load_meta(uc)
        problems += [f"{uc.name}/usecase.toml: {e.message}" for e in uc_schema.iter_errors(meta)]
        if meta.get("id") != uc.name:
            problems.append(f"{uc.name}/usecase.toml: id must equal the directory name")
        if meta.get("reference") not in meta.get("languages", []):
            problems.append(f"{uc.name}/usecase.toml: reference must be one of languages")
        for lang in meta.get("languages", []):
            if not (uc / lang).is_dir():
                problems.append(f"{uc.name}: language {lang} is listed but {lang}/ does not exist")
        if (uc / "output.schema.json").exists():
            jsonschema.Draft202012Validator.check_schema(json.loads((uc / "output.schema.json").read_text()))
        for size, s in meta.get("sizes", {}).items():
            mf = uc / s["manifest"]
            if not mf.exists():
                problems.append(f"{uc.name}: {size} manifest {s['manifest']} is missing")
                continue
            doc = json.loads(mf.read_text())
            problems += [f"{uc.name}/{s['manifest']}: {e.message}" for e in man_schema.iter_errors(doc)]
            for it in doc.get("items", []) if size == "smoke" else []:
                img = mf.parent / it.get("image", "")
                if "image" in it and not img.is_file():
                    problems.append(f"{uc.name}: {it['image']} of item {it['id']} is missing")
                elif "sha256" in it and sha(img) != it["sha256"]:
                    problems.append(f"{uc.name}: sha256 of {it['image']} does not match the manifest")
    return problems


def check_attribution():
    """Every asset file appears in an ATTRIBUTION*.csv of its use case's assets/ or in ATTRIBUTION.md."""
    problems, root_text = [], (ROOT / "ATTRIBUTION.md").read_text(encoding="utf-8") if (ROOT / "ATTRIBUTION.md").exists() else ""
    for uc in use_cases():
        assets = uc / "assets"
        listed = root_text + "".join(p.read_text(encoding="utf-8") for p in assets.rglob("ATTRIBUTION*.csv"))
        for f in sorted(assets.rglob("*")):
            if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
                if f.relative_to(assets).as_posix() not in listed and f"{uc.name}/assets/{f.relative_to(assets).as_posix()}" not in listed:
                    problems.append(f"{uc.name}: assets/{f.relative_to(assets).as_posix()} has no attribution row")
    return problems


def lint():
    problems = []
    tools = tomllib.loads((ROOT / "harness" / "languages.toml").read_text())
    p = subprocess.run("ruff check harness clients tests && ruff format --check harness clients tests", shell=True, cwd=ROOT, capture_output=True, text=True)
    if p.returncode:
        problems.append("ruff (harness, clients, tests):\n" + (p.stdout + p.stderr)[-2000:])
    for uc in use_cases():
        for lang in load_meta(uc)["languages"]:
            for need in tools[lang].get("requires", []):
                if not shutil.which(need):
                    problems.append(f"{uc.name}/{lang}: needs `{need}` on PATH")
            p = subprocess.run(tools[lang]["lint"].format(dir=str(uc / lang)), shell=True, cwd=uc / lang, capture_output=True, text=True)
            if p.returncode:
                problems.append(f"{uc.name}/{lang} lint:\n" + (p.stdout + p.stderr)[-2000:])
    return problems


def cmd_check(args):
    steps = [
        ("contract", check_contract),
        (
            "client copies",
            lambda: [
                f"{d.relative_to(ROOT)} differs from {s.relative_to(ROOT)}: run `harness sync-clients`"
                for s, d in copies()
                if not d.exists() or sha(s) != sha(d)
            ],
        ),
        ("attribution", check_attribution),
        ("lint", lint),
        ("snippets", lambda: snippets.run(check=True)),
        ("i18n", i18n.check),
        ("scan", scan.run),
    ]
    if not args.skip_tests:
        steps.append(
            (
                "tests (clients, fake API, harness)",
                lambda: [] if subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"], cwd=ROOT).returncode == 0 else ["pytest failed"],
            )
        )
    steps.append(("offline matrix", lambda: matrix.offline(args.uc, args.lang, args.write_offline)))
    failed = 0
    for name, fn in steps:
        print(f"== {name}", flush=True)
        problems = fn()
        for p in problems:
            print(f"  FAIL {p}")
        failed += bool(problems)
    print("check: " + ("all green" if not failed else f"{failed} step(s) failed"))
    return 1 if failed else 0


def cmd_live(args, write=False):
    code, problems = matrix.live(args.size, args.uc, args.lang, args.max_credits, args.dry_run, write, getattr(args, "record_fixtures", False))
    for p in problems:
        print(("error: " if code == 2 else "FAIL ") + p, file=sys.stderr)
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m harness", description="Test harness of the Foldbun Stack Cookbook.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    csv = lambda s: [x.strip() for x in s.split(",") if x.strip()]
    c = sub.add_parser("check", help="everything offline, no key needed")
    c.add_argument("--uc", type=csv, help="use cases, e.g. 00,03")
    c.add_argument("--lang", type=csv, help="languages, e.g. python,go")
    c.add_argument("--skip-tests", action="store_true", help="skip pytest")
    c.add_argument("--write-offline", action="store_true", help="write expected/offline.json from the reference language")
    for name, helptext in (("live", "run against the real API (needs FOLDBUN_API_KEY)"), ("refresh", "live, then write expected/ (needs FOLDBUN_API_KEY)")):
        p = sub.add_parser(name, help=helptext, description=helptext + ". The planned list price is checked against the cap before anything is sent.")
        p.add_argument("--size", choices=["smoke", "full"], default="smoke")
        p.add_argument("--uc", type=csv)
        p.add_argument("--lang", type=csv)
        p.add_argument("--max-credits", type=float, help="cap for the whole invocation (smoke default: FOLDBUN_MAX_CREDITS or 60; required for full)")
        p.add_argument("--dry-run", action="store_true", help="print the plan and stop")
        if name == "refresh":
            p.add_argument("--record-fixtures", action="store_true", help="also record the responses as fixtures for the fake API")
    sub.add_parser("snippets", help="expand marker blocks in README files")
    i = sub.add_parser("i18n", help="check the English / Chinese README pairs")
    i.add_argument("--accept", metavar="UC", help="accept the current English text of UC (or root) as translated")
    sub.add_parser("scan", help="publication scan")
    sub.add_parser("sync-clients", help="copy clients/<lang>/ files over every copy")
    sub.add_parser("fakeapi", help="run the fake API", add_help=False)
    args, rest = ap.parse_known_args(argv)
    if args.cmd == "fakeapi":
        return fakeapi.main(rest)
    if rest:
        ap.error(f"unrecognised arguments: {' '.join(rest)}")
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd in ("live", "refresh"):
        return cmd_live(args, write=args.cmd == "refresh")
    if args.cmd == "snippets":
        problems = snippets.run(check=False)
    elif args.cmd == "i18n":
        problems = i18n.accept(args.accept) if args.accept else i18n.check()
    elif args.cmd == "scan":
        problems = scan.run()
    else:
        for s, d in copies():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(s, d)
            print(f"  {d.relative_to(ROOT)}")
        problems = []
    for p in problems:
        print(f"FAIL {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
