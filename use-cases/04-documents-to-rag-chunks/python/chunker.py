"""Layout blocks of a document's pages to RAG chunks: drop repeated headers and footers, then cut the text into chunks.

Pure functions, no network. `main.py` reads the pages; this file decides what a chunk is.
"""

import re

TARGET = 800  # characters per chunk, about
ATOMIC = ("table", "formula", "figure")  # never cut; they go into a chunk whole
FURNITURE = ("header", "footer")
SENTENCE = re.compile(r"(?<=[.!?;:])\s+(?=\S)")


def squash(text: str) -> str:
    """Whitespace runs (including the line breaks inside a block) become single spaces."""
    return re.sub(r"\s+", " ", text or "").strip()


# region dedup
def furniture_key(block: dict) -> str:
    """The text of a header or footer with its digits blanked, so "Page 2 of 3" and "Page 3 of 3" are the same line."""
    return re.sub(r"\d+", "#", squash(block.get("text", ""))).lower()


def mark_repeated(pages: list[dict]) -> int:
    """Flag the header and footer blocks whose text appears on at least two pages of the document.

    `pages` are the successfully read pages of ONE document: {"page": n, "blocks": [...]}. A flagged block gets
    `dropped: "repeated_header_footer"` and is left out of the chunks and of document.md. A header seen on one page
    only is kept: nothing says it is page furniture. Returns the number of flagged blocks."""
    seen: dict[tuple[str, str], set[int]] = {}
    for page in pages:
        for block in page["blocks"]:
            if block["type"] in FURNITURE and furniture_key(block):
                seen.setdefault((block["type"], furniture_key(block)), set()).add(page["page"])
    flagged = 0
    for page in pages:
        for block in page["blocks"]:
            block.pop("dropped", None)
            if block["type"] in FURNITURE and len(seen.get((block["type"], furniture_key(block)), ())) >= 2:
                block["dropped"] = "repeated_header_footer"
                flagged += 1
    return flagged


# endregion


def heading_level(block: dict) -> int:
    """`#` count of the block's Markdown; 2 when the model gave no marker."""
    m = re.match(r"(#{1,6})\s", block.get("markdown") or "")
    return len(m.group(1)) if m else 2


def block_text(block: dict) -> str:
    """The text a block contributes to a chunk: tables and formulas as the API gave them, titles as Markdown headings."""
    if block["type"] in ("table", "formula"):
        return (block.get("markdown") or block.get("text") or "").strip()
    text = squash(block.get("text", ""))
    if block["type"] == "title":
        return "#" * heading_level(block) + " " + text
    return text


# region split-text
def split_text(text: str, limit: int) -> list[str]:
    """Pieces of at most `limit` characters, cut at sentence ends; a sentence longer than the limit is cut at a space."""
    pieces, cur = [], ""
    for sentence in SENTENCE.split(text):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if cur and len(cur) + 1 + len(sentence) > limit:
            pieces.append(cur)
            cur = ""
        cur = f"{cur} {sentence}".strip()
    return pieces + [cur] if cur else pieces


# endregion


# region chunk-page
def chunk_page(blocks: list[dict], page: int, headings: list[tuple[int, str]], target: int = TARGET) -> tuple[list[dict], list[tuple[int, str]]]:
    """Chunks of one page. `headings` is the stack of (level, text) the previous pages left open; the new stack is returned.

    - A title closes the chunk in progress and opens a new one, which starts with the heading.
    - Text is packed up to `target` characters; a block longer than that is cut at sentence ends first.
    - Tables and formulas are never cut. One that does not fit in the chunk in progress starts a new chunk, and a table
      longer than `target` is a chunk of its own.
    - A chunk never crosses a page boundary, so `page` and `bbox` are exact.
    """
    chunks: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur["parts"]:
            chunks.append(cur)
        cur = None

    def open_chunk():
        nonlocal cur
        cur = {"parts": [], "blocks": [], "types": [], "boxes": [], "headings": [t for _, t in headings]}

    def add(text: str, block: dict):
        nonlocal cur
        if cur is None:
            open_chunk()
        cur["parts"].append(text)
        if [block["order"]] not in [b[1:] for b in cur["blocks"]]:
            cur["blocks"].append([page, block["order"]])
        if block["type"] not in cur["types"]:
            cur["types"].append(block["type"])
        cur["boxes"].append(block["bbox"])

    def size() -> int:
        return len("\n\n".join(cur["parts"])) if cur else 0

    for block in sorted((b for b in blocks if not b.get("dropped")), key=lambda b: b["order"]):
        text = block_text(block)
        if not text:
            continue
        if block["type"] == "title":
            level = heading_level(block)
            headings = [h for h in headings if h[0] < level] + [(level, squash(block["text"]))]
            flush()
            open_chunk()
            add(text, block)
        elif block["type"] in ATOMIC:
            if cur and size() + 2 + len(text) > target:
                flush()
            add(text, block)
            if len(text) > target:
                flush()
        else:
            for piece in split_text(text, target):
                if cur and size() + 2 + len(piece) > target:
                    flush()
                add(piece, block)
    flush()
    out = []
    for c in chunks:
        xs = [b[0] for b in c["boxes"]], [b[1] for b in c["boxes"]], [b[2] for b in c["boxes"]], [b[3] for b in c["boxes"]]
        text = "\n\n".join(c["parts"])
        out.append(
            {
                "page": page,
                "text": text,
                "chars": len(text),
                "headings": c["headings"],
                "blockTypes": c["types"],
                "bbox": [min(xs[0]), min(xs[1]), max(xs[2]), max(xs[3])],
                "blocks": c["blocks"],
            }
        )
    return out, headings


# endregion


def chunk_document(doc_id: str, pages: list[dict], sha256: dict[int, str], target: int = TARGET) -> list[dict]:
    """All chunks of a document in page order, numbered `<doc>-c0001`..., each with the sha256 of its page's file."""
    headings: list[tuple[int, str]] = []
    chunks: list[dict] = []
    for page in sorted(pages, key=lambda p: p["page"]):
        got, headings = chunk_page(page["blocks"], page["page"], headings, target)
        for c in got:
            chunks.append({"id": f"{doc_id}-c{len(chunks) + 1:04d}", "document": doc_id, **c, "sha256": sha256[page["page"]]})
    return chunks
