"""Metrics of a click-simulator run: evaluate.py --result <result.json> --truth <truth.json>.

The simulator (harness/clickbot.py) already compares every returned polygon with the ground-truth mask and stores the
IoU after each click in result.json, so the metrics need only result.json; `--truth` is accepted for the common
evaluate.py interface. Prints one JSON object on its last line: NoC@85 / NoC@90 (mean, and the share of objects that
reached the threshold), mean IoU after k clicks, credits per object, cache hit rate. Network timing is not reported.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.clickbot import summarise  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--result", required=True)
    ap.add_argument("--truth", help="not needed: the IoU per click is in result.json")
    args = ap.parse_args()
    print(json.dumps(summarise(json.loads(Path(args.result).read_text()))))


if __name__ == "__main__":
    main()
