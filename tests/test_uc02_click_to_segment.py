"""Use case 02: the click simulator's geometry and metrics, and the backend's own tests in each language."""

import json
import subprocess
import sys

import numpy as np
import pytest
from conftest import ROOT

from harness import clickbot

UC = ROOT / "use-cases" / "02-click-to-segment"


def test_first_click_is_the_deepest_point():
    m = np.zeros((40, 60), dtype=bool)
    m[10:31, 5:46] = True  # 21 x 41: the centre is (25, 20)
    assert clickbot.next_click(m, np.zeros_like(m), first=True) == (25, 20, True)


def test_later_clicks_go_to_the_largest_error_region():
    truth = np.zeros((40, 60), dtype=bool)
    truth[5:35, 5:25] = True
    pred = truth.copy()
    pred[5:35, 30:55] = True  # excess, larger than what is missed
    pred[5:12, 5:25] = False  # missed
    x, y, positive = clickbot.next_click(truth, pred, first=False)
    assert not positive and 30 <= x < 55 and 5 <= y < 35
    pred[5:35, 30:55] = False
    x, y, positive = clickbot.next_click(truth, pred, first=False)
    assert positive and 5 <= x < 25 and 5 <= y < 12
    assert clickbot.next_click(truth, truth, first=False) is None


def test_rasterise_keeps_outline_and_clears_holes():
    m = clickbot.rasterise([{"outer": [0, 0, 9, 0, 9, 9, 0, 9], "holes": [[3, 3, 3, 6, 6, 6, 6, 3]]}], 12, 12)
    assert m[0, 0] and m[9, 9] and not m[10, 10]
    assert m[3, 3] and not m[4, 4]
    assert m.sum() == 100 - 4


def test_summarise_counts_unreached_thresholds_as_max_clicks():
    click = lambda iou, cache: {"iou": iou, "cache": cache, "listCredits": 0.01, "costCredits": 0}
    out = lambda clicks, n85, n90: {"maxClicks": 4, "clicks": clicks, "noc85": n85, "noc90": n90}
    result = {
        "items": [
            {"status": "ok", "output": out([click(0.5, "miss"), click(0.92, "hit")], 2, 2)},
            {"status": "ok", "output": out([click(0.3, "miss"), click(0.6, "hit"), click(0.86, "hit"), click(0.88, "hit")], 3, None)},
        ]
    }
    m = clickbot.summarise(result)
    assert m["noc85_mean"] == 2.5 and m["noc90_mean"] == 3.0 and m["noc90_reached"] == 0.5
    assert m["miou_at_1"] == 0.4 and m["miou_at_4"] == 0.9  # 0.92 carries forward
    assert m["cache_hit_rate"] == round(4 / 6, 4)


def test_evaluate_prints_the_summary():
    expected = json.loads((UC / "expected" / "offline.json").read_text())
    p = subprocess.run([sys.executable, "evaluate.py", "--result", str(UC / "expected" / "offline.json")], cwd=UC, capture_output=True, text=True, check=True)
    assert json.loads(p.stdout.strip().splitlines()[-1]) == clickbot.summarise(expected)


@pytest.mark.parametrize("lang", ["typescript", "python", "go"])
def test_backend_unit_tests(lang):
    impl = UC / lang
    if lang == "typescript":
        subprocess.run(["pnpm", "--dir", str(impl), "install", "--frozen-lockfile"], cwd=impl, check=True, capture_output=True)
        p = subprocess.run(["node", "--test"], cwd=impl, capture_output=True, text=True)
    elif lang == "python":  # the standard library plus pytest, which the harness environment has
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=impl, capture_output=True, text=True)
    else:
        p = subprocess.run(["go", "test", "./..."], cwd=impl, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]


def test_web_component_builds_and_its_unit_tests_pass():
    web = UC / "web"
    subprocess.run(["pnpm", "--dir", str(web), "install", "--frozen-lockfile"], cwd=web, check=True, capture_output=True)
    build = subprocess.run(["pnpm", "run", "build"], cwd=web, capture_output=True, text=True)
    assert build.returncode == 0, build.stdout[-3000:] + build.stderr[-2000:]
    assert (web / "dist" / "index.html").is_file()
    p = subprocess.run(["node", "--test"], cwd=web, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]
