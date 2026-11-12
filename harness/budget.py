"""Credit planning for live runs: budgets come from usecase.toml; per-call estimates are made by the client files."""

import os

SMOKE_DEFAULT_CAP = 60.0


class BudgetError(Exception):
    pass


def size_budget(meta, size):
    s = meta["sizes"].get(size)
    return None if s is None else float(s["budget_credits"])


def size_languages(meta, size):
    langs = meta["sizes"][size].get("languages") or meta["languages"]
    ref = meta["reference"]
    return sorted(langs, key=lambda lang: (lang != ref, meta["languages"].index(lang)))  # reference language first


def cap(size, max_credits):
    """The credit cap of a whole live invocation: smoke defaults to FOLDBUN_MAX_CREDITS or 60; full must be explicit."""
    if max_credits is not None:
        return float(max_credits)
    if size == "full":
        raise BudgetError("--size full needs an explicit --max-credits")
    env = os.environ.get("FOLDBUN_MAX_CREDITS")
    return float(env) if env else SMOKE_DEFAULT_CAP


def plan(runs, limit):
    """`runs` is a list of (usecase id, language, list-price budget). Refuses when the sum passes `limit`."""
    total = round(sum(b for _, _, b in runs), 6)
    if total > limit + 1e-9:
        raise BudgetError(f"planned list price {total:g} credits exceeds the cap of {limit:g}; narrow --uc / --lang or raise --max-credits")
    return total
