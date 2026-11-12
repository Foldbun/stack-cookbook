"""Compare two result.json documents (language against language, run against expected/).

Always compared: the item ids, each item's status, and per item the number, endpoints and models of its calls.
Then each `[compare]` rule of usecase.toml, keyed by a path into result.json: `items[]` pairs items by `id`, any other
`name[]` pairs list elements by position, and a plain name descends into an object, e.g. `items[].output.lines[].text`.
Rules: `exact`, `abs` / `rel` (numbers, or lists of numbers), `len` (list lengths), `set` (same elements in any order),
`match_iou` (lists of [x1, y1, x2, y2] boxes matched one to one at that IoU) and `ignore`. When no rule starts with
`items[].output`, outputs are compared exactly.
"""


def _walk(node, parts, label):
    """Yield (label, value) for every value a path reaches; pairs are matched by the caller."""
    if not parts:
        yield label, node
        return
    head, rest = parts[0], parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        seq = node.get(key) if isinstance(node, dict) else None
        for i, el in enumerate(seq or []):
            tag = el.get("id", i) if key == "items" and isinstance(el, dict) else i
            yield from _walk(el, rest, f"{label}{key}[{tag}].")
    elif isinstance(node, dict) and head in node:
        yield from _walk(node[head], rest, f"{label}{head}.")
    else:
        yield f"{label}{head}.", _MISSING


_MISSING = object()


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 1.0


def _num_close(x, y, abs_tol, rel_tol):
    if isinstance(x, list) and isinstance(y, list):
        return len(x) == len(y) and all(_num_close(p, q, abs_tol, rel_tol) for p, q in zip(x, y))
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return abs(x - y) <= max(abs_tol, rel_tol * max(abs(x), abs(y)))
    return x == y


def _apply(rule, x, y):
    if "ignore" in rule:
        return True
    if x is _MISSING or y is _MISSING:
        return x is y
    if "exact" in rule:
        return x == y
    if "abs" in rule or "rel" in rule:
        return _num_close(x, y, rule.get("abs", 0), rule.get("rel", 0))
    if "len" in rule:
        return isinstance(x, list) and isinstance(y, list) and len(x) == len(y)
    if "set" in rule:
        return sorted(map(repr, x)) == sorted(map(repr, y))
    if "match_iou" in rule:
        if len(x) != len(y):
            return False
        left = list(y)
        for box in x:
            best = max(left, key=lambda b: _iou(box, b), default=None)
            if best is None or _iou(box, best) < rule["match_iou"]:
                return False
            left.remove(best)
        return True
    raise ValueError(f"unknown compare rule {rule}")


def compare(a, b, rules=None, names=("a", "b")):
    """Differences between result documents `a` and `b` as human-readable strings (empty when they agree)."""
    rules = dict(rules or {})
    diffs = []
    ia, ib = {i["id"]: i for i in a["items"]}, {i["id"]: i for i in b["items"]}
    if list(ia) != list(ib):
        diffs.append(f"item ids differ: {names[0]} {list(ia)} vs {names[1]} {list(ib)}")
    for iid in ia.keys() & ib.keys():
        x, y = ia[iid], ib[iid]
        if x["status"] != y["status"]:
            diffs.append(f"items[{iid}].status: {x['status']} vs {y['status']}")
        shape = lambda it: [(c["endpoint"], c["model"]) for c in it["calls"]]
        if shape(x) != shape(y):
            diffs.append(f"items[{iid}].calls: {shape(x)} vs {shape(y)}")
    if not any(p.startswith("items[].output") for p in rules):
        rules["items[].output"] = {"exact": True}
    for path, rule in rules.items():
        parts = path.split(".")
        va, vb = dict(_walk(a, parts, "")), dict(_walk(b, parts, ""))
        for label in sorted(va.keys() | vb.keys()):
            x, y = va.get(label, _MISSING), vb.get(label, _MISSING)
            if path.startswith("items[]") and (x is _MISSING or y is _MISSING) and label.split(".")[0] not in {f"items[{i}]" for i in ia.keys() & ib.keys()}:
                continue  # missing items are reported above
            if not _apply(rule, x, y):
                show = lambda v: "(missing)" if v is _MISSING else repr(v)[:160]
                diffs.append(f"{label.rstrip('.')} [{next(iter(rule))}]: {names[0]} {show(x)} vs {names[1]} {show(y)}")
    return diffs
