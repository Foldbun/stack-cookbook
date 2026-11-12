"""Run implementations: the offline matrix against the fake API, and live runs against the real API."""

import json
import os
import shutil
import subprocess
import time
import tomllib

import jsonschema

from harness import budget
from harness.compare import compare
from harness.fakeapi import FakeServer
from harness.snippets import ROOT, load_meta, use_cases

LANGUAGES = tomllib.loads((ROOT / "harness" / "languages.toml").read_text())
RUNS = ROOT / "tmp" / "runs"
EXIT_TIER = 3  # 403 model_locked: the account tier cannot run this use case; recorded as a skip


def contract(name):
    return json.loads((ROOT / "contract" / f"{name}.schema.json").read_text())


def selected(ucs=None, langs=None, size="smoke"):
    """[(uc_dir, meta, language)] in run order: per use case, the reference language first."""
    out = []
    for uc in use_cases():
        meta = load_meta(uc)
        if ucs and uc.name not in ucs and uc.name[:2] not in ucs:
            continue
        if size not in meta["sizes"]:
            continue
        out += [(uc, meta, lang) for lang in budget.size_languages(meta, size) if not langs or lang in langs]
    return out


def sh(cmd, cwd, env, timeout):
    p = subprocess.run(cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)[-3000:]


def run_impl(uc, meta, lang, size, env, timeout=900, live=False):
    """Set up and run one implementation. Returns (exit code, output tail, result dict or None, out dir).

    A use case of `kind = "server"` has HTTP backends instead of commands: harness/clickbot.py starts `run --port N`,
    drives it over the manifest and writes result.json itself."""
    tool = LANGUAGES[lang]
    impl = uc / lang
    out = RUNS / size / uc.name / lang
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    fill = lambda c: c.format(dir=str(impl))
    if tool.get("setup"):
        code, tail = sh(fill(tool["setup"]), impl, env, timeout)
        if code:
            return code, "setup failed:\n" + tail, None, out
    manifest = uc / meta["sizes"][size]["manifest"]
    if meta.get("kind") == "server":
        from harness import clickbot

        code, tail = clickbot.run(fill(tool["run"]), impl, env, manifest, out, uc.name, lang, live=live)
    else:
        code, tail = sh(f"{fill(tool['run'])} --manifest {manifest} --out {out}", impl, env, timeout)
    result_file = out / "result.json"
    result = json.loads(result_file.read_text()) if result_file.exists() else None
    return code, tail, result, out


def validate_result(uc, meta, lang, result):
    problems = [f"result.json: {e.message}" for e in jsonschema.Draft202012Validator(contract("result")).iter_errors(result)]
    if result.get("usecase") != uc.name or result.get("language") != lang:
        problems.append(f"result.json: usecase/language are {result.get('usecase')}/{result.get('language')}")
    out_schema = uc / "output.schema.json"
    if out_schema.exists():
        v = jsonschema.Draft202012Validator(json.loads(out_schema.read_text()))
        for it in result.get("items", []):
            if it.get("status") == "ok":
                problems += [f"items[{it['id']}].output: {e.message}" for e in v.iter_errors(it["output"])]
    n = sum(len(it["calls"]) for it in result.get("items", []))
    if result.get("ledger", {}).get("calls") != n:
        problems.append(f"ledger.calls is {result.get('ledger', {}).get('calls')} but items hold {n} calls")
    return problems


def offline(ucs=None, langs=None, write=False, log=print):
    """Run every selected implementation's smoke set against the fake API; compare with expected/offline.json."""
    problems = []
    fixtures = [str(p) for p in sorted(ROOT.glob("use-cases/*/fixtures"))]
    with FakeServer(fixtures=fixtures, validate=True) as srv:
        results = {}
        for uc, meta, lang in selected(ucs, langs):
            key = "fbs_fake_public" if meta["tier"] == "public" else "fbs_fake"
            env = dict(os.environ, FOLDBUN_API_BASE=srv.url, FOLDBUN_API_KEY=key, FOLDBUN_MAX_CREDITS=str(budget.size_budget(meta, "smoke")))
            t = time.monotonic()
            with srv.fake.lock:
                srv.fake.encoded.clear()  # every implementation starts with a cold image-encoding cache, whatever ran before it
            code, tail, result, _ = run_impl(uc, meta, lang, "smoke", env)
            tag = f"{uc.name}/{lang}"
            if code or result is None:
                problems.append(f"{tag}: exit {code}\n{tail}")
                continue
            errs = validate_result(uc, meta, lang, result)
            expected = uc / "expected" / "offline.json"
            rules = meta.get("compare", {})
            if lang == meta["reference"] and write:
                expected.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
            if expected.exists():
                errs += compare(json.loads(expected.read_text()), result, rules, ("expected", lang))
            else:
                errs.append("expected/offline.json is missing: run `harness check --write-offline`")
            if uc.name in results:
                errs += compare(results[uc.name], result, rules, (meta["reference"], lang))
            results.setdefault(uc.name, result)
            problems += [f"{tag}: {e}" for e in errs]
            log(f"  {tag}: {'ok' if not errs else 'FAILED'} ({time.monotonic() - t:.1f} s)")
        violations = json.loads(_get(srv.url + "/_fake/log"))["violations"]
        problems += [f"API description: {v}" for v in violations]
    return problems


def _get(url):
    import urllib.request

    with urllib.request.urlopen(url) as r:
        return r.read()


def live(size, ucs, langs, max_credits, dry_run=False, write=False, record=False, log=print):
    """Live matrix. Returns (exit code, problems)."""
    if not os.environ.get("FOLDBUN_API_KEY"):
        return 2, ["FOLDBUN_API_KEY is not set. Live runs call the real API and need a key; `harness check` runs everything offline."]
    runs = [(uc, meta, lang, budget.size_budget(meta, size)) for uc, meta, lang in selected(ucs, langs, size)]
    if not runs:
        return 1, ["nothing selected"]
    try:
        limit = budget.cap(size, max_credits)
        total = budget.plan([(uc.name, lang, b) for uc, _, lang, b in runs], limit)
    except budget.BudgetError as exc:
        return 1, [str(exc)]
    log(f"plan: {len(runs)} runs, at most {total:g} credits at list price (cap {limit:g})")
    for uc, _, lang, b in runs:
        log(f"  {uc.name}/{lang}: up to {b:g}")
    if dry_run:
        return 0, []
    base = os.environ.get("FOLDBUN_API_BASE", "https://api.stack.foldbun.com")
    proxy = FakeServer(upstream=base, record=None) if record else None
    if proxy:
        proxy.__enter__()
    spent, problems, results = 0.0, [], {}
    try:
        for uc, meta, lang, b in runs:
            remaining = limit - spent
            env = dict(os.environ, FOLDBUN_MAX_CREDITS=str(min(b, remaining)))
            if proxy:
                env["FOLDBUN_API_BASE"] = proxy.url
            if proxy:
                proxy.fake.recorded.clear()
            code, tail, result, out = run_impl(uc, meta, lang, size, env, live=True)
            tag = f"{uc.name}/{lang}"
            if proxy and lang == meta["reference"] and proxy.fake.recorded and not code:
                (uc / "fixtures").mkdir(exist_ok=True)
                (uc / "fixtures" / "recorded.json").write_text(json.dumps(proxy.fake.recorded, indent=1) + "\n")
            if code == EXIT_TIER:
                log(f"  {tag}: skipped (the account tier cannot run it)")
                continue
            if code or result is None:
                problems.append(f"{tag}: exit {code}\n{tail}")
                continue
            spent += result["ledger"]["listTotal"]
            errs = validate_result(uc, meta, lang, result)
            # [compare_live] overrides [compare] for runs against the real API (e.g. cache state, which it does not promise)
            rules = {**meta.get("compare", {}), **meta.get("compare_live", {})}
            expected = uc / "expected" / f"{size}.json"
            if expected.exists() and not write:
                errs += compare(json.loads(expected.read_text()), result, rules, ("expected", lang))
            if uc.name in results:
                errs += compare(results[uc.name], result, rules, (meta["reference"], lang))
            results.setdefault(uc.name, result)
            if size == "full" and (uc / "evaluate.py").exists() and lang == meta["reference"]:
                errs += evaluate(uc, meta, out, write)
            if write and lang == meta["reference"]:
                expected.parent.mkdir(exist_ok=True)
                expected.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
            problems += [f"{tag}: {e}" for e in errs]
            log(f"  {tag}: {'ok' if not errs else 'FAILED'}, {result['ledger']['listTotal']:g} list / {result['ledger']['chargedTotal']:g} charged")
    finally:
        if proxy:
            proxy.__exit__(None, None, None)
    log(f"spent {spent:g} credits at list price")
    return (1 if problems else 0), problems


def evaluate(uc, meta, out, write):
    """evaluate.py contract: `uv run python evaluate.py --result R --truth T` prints a flat JSON object of metrics."""
    truth = (uc / meta["sizes"]["full"]["manifest"]).parent / "truth.json"
    code, tail = sh(f"uv run --project {ROOT} python evaluate.py --result {out / 'result.json'} --truth {truth}", uc, os.environ.copy(), 600)
    if code:
        return [f"evaluate.py failed:\n{tail}"]
    metrics = json.loads(tail.strip().splitlines()[-1])
    if write:
        (uc / "expected" / "full-summary.json").write_text(json.dumps(metrics, indent=2) + "\n")
    errs = []
    for name, bound in meta.get("quality", {}).items():
        v = metrics.get(name)
        if v is None or v < bound.get("min", float("-inf")) or v > bound.get("max", float("inf")):
            errs.append(f"quality {name} = {v} outside {bound}")
    return errs
