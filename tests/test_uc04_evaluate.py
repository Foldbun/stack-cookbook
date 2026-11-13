"""Unit tests of 04-documents-to-rag-chunks/evaluate.py: edit distance, table grids, reading order, and the metrics of a small run."""

import importlib.util
import json
import random
from pathlib import Path

UC = Path(__file__).resolve().parent.parent / "use-cases" / "04-documents-to-rag-chunks"
spec = importlib.util.spec_from_file_location("uc04_evaluate", UC / "evaluate.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def slow_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def test_bit_parallel_edit_distance_agrees_with_the_textbook_one():
    rng = random.Random(7)
    for _ in range(200):
        a = "".join(rng.choice("abc ") for _ in range(rng.randint(0, 40)))
        b = "".join(rng.choice("abc ") for _ in range(rng.randint(0, 40)))
        assert ev.edit_distance(a, b) == slow_distance(a, b)


def test_ned_ignores_whitespace_quotes_and_dashes():
    assert ev.ned("He said “hi” — twice", 'He  said "hi" - twice') == 0
    assert ev.ned("abcd", "abce") == 0.25


def test_table_grid_expands_spans_and_matches_the_truth_matrix():
    html = "<table><tr><td>Item</td><td colspan='2'>Total</td></tr><tr><td rowspan='2'>A</td><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>"
    g = ev.grid(html)
    assert g[(0, 1)] == g[(0, 2)] == "total" or g[(0, 1)] == g[(0, 2)] == "Total"
    assert g[(1, 0)] == g[(2, 0)] == "A"
    truth = ev.grid_of_matrix([["Item", "Total", "Total"], ["A", "1", "2"], ["A", "3", "4"]])
    assert ev.cell_counts(g, truth) == (9, 9, 9)


def test_kendall_tau_and_reading_order():
    assert ev.kendall_tau([0, 1, 2, 3]) == 1.0
    assert ev.kendall_tau([3, 2, 1, 0]) == -1.0
    truth = [{"order": i, "type": "text", "bbox": [0, 100 * i, 500, 100 * i + 80]} for i in range(3)]
    pred = [
        {"order": 0, "type": "text", "bbox": [0, 200, 500, 280]},
        {"order": 1, "type": "text", "bbox": [0, 0, 500, 80]},
        {"order": 2, "type": "text", "bbox": [0, 100, 500, 180]},
    ]
    assert ev.reading_order(pred, truth) == ev.kendall_tau([2, 0, 1])
    assert ev.reading_order(truth, truth) == 1.0


def test_metrics_of_the_recorded_offline_run():
    result = json.loads((UC / "expected" / "offline.json").read_text())
    truth = json.loads((UC / "assets" / "smoke" / "truth.json").read_text())
    m = ev.evaluate(result, truth)
    assert m["pages"] == m["pages_scored"] == 2 and m["pages_failed"] == 0
    assert m["tables_split"] == 0 and m["tables_in_chunks"] == 2
    assert m["table_cell_f1"] == 1.0 and m["text_ned_clean"] < 0.01
    assert m["cost_per_1000_pages_list"] == 2000.0
    assert not any("latency" in k or "throughput" in k for k in m)
