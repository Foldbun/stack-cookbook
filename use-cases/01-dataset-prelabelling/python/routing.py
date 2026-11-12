"""Turn the API's instances into labels, and decide which ones a person has to look at.

Three bands, by `score` (for text prompts the detection confidence, for boxes the predicted mask IoU):

* `score >= accept`            the label is exported as it is;
* `review <= score < accept`   the label is kept in the review queue, with a crop;
* no instance of a class an image is known to contain (`expectedClasses` in the manifest): the image goes to the review
  queue for that class, because a silent miss is the failure that pre-labelling hides best.

The request asks the API for everything above `review` (`filter.minScore`), so nothing below it is ever returned.
"""

from dataclasses import dataclass, field


@dataclass
class Detection:
    cls: str
    score: float
    bbox: list  # [x1, y1, x2, y2], x2 and y2 exclusive, image pixels
    area: int
    route: str  # "accept" or "review"
    polygon: list = field(default_factory=list)  # rings of [x, y, ...] at pixel centres; outer rings only
    mask: dict | None = None  # COCO RLE on the mask grid, only with --masks
    prompt: int = 0


# region routing
def route(score, cfg):
    """The band of a score: "accept", "review", or None when it is below the review threshold."""
    if score >= cfg.accept:
        return "accept"
    if score >= cfg.review or cfg.mode == "boxes":  # a box the user supplied is never dropped, only checked
        return "review"
    return None


def detections(unit, result, cfg):
    """Detections of one item (sorted by score), and the expected classes for which nothing was found."""
    found = []
    for inst in result["instances"]:
        band = route(inst["score"], cfg)
        if band is None:
            continue
        idx = inst.get("prompt", 0)
        rings = [part["outer"] for part in inst.get("polygon", [])]
        found.append(Detection(unit.labels[idx], inst["score"], inst["bbox"], inst["area"], band, rings, inst.get("mask") if cfg.masks else None, idx))
    found.sort(key=lambda d: (-d.score, d.bbox))
    seen = {d.cls for d in found}
    missing = [c for c in unit.item.get("expectedClasses", []) if c not in seen]
    return found, missing


# endregion
