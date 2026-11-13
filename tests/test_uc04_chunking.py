"""Unit tests of the pure parts of 04-documents-to-rag-chunks/python: header and footer removal, heading paths, chunk boundaries, tables."""

import json
import sys
from pathlib import Path

import pytest

UC = Path(__file__).resolve().parent.parent / "use-cases" / "04-documents-to-rag-chunks"
sys.path.insert(0, str(UC / "python"))

import chunker  # noqa: E402


def block(order, typ, text, markdown=None, box=(100, 100, 900, 200)):
    return {"type": typ, "order": order, "bbox": list(box), "text": text, "markdown": markdown if markdown is not None else text}


def title(order, text, level=2):
    return block(order, "title", text, "#" * level + " " + text)


def table(order, rows):
    html = "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"
    return block(order, "table", html)


def sentence_text(n_chars):
    s = "This sentence is about forty characters. "
    return (s * (n_chars // len(s) + 1))[:n_chars].strip()


def test_header_footer_dedup_across_pages():
    pages = [
        {"page": p, "blocks": [block(0, "header", "Wealth of Nations"), block(1, "text", f"Body {p}"), block(2, "footer", f"Page {p} of 3")]} for p in (1, 2, 3)
    ]
    assert chunker.mark_repeated(pages) == 6
    for page in pages:
        assert [b.get("dropped") for b in page["blocks"]] == ["repeated_header_footer", None, "repeated_header_footer"]


def test_header_on_one_page_is_kept_and_body_text_is_never_dropped():
    pages = [{"page": 1, "blocks": [block(0, "header", "Only here"), block(1, "text", "Same body")]}, {"page": 2, "blocks": [block(0, "text", "Same body")]}]
    assert chunker.mark_repeated(pages) == 0
    assert not any(b.get("dropped") for p in pages for b in p["blocks"])


def test_dropped_blocks_stay_out_of_chunks():
    blocks = [{**block(0, "header", "Running head"), "dropped": "repeated_header_footer"}, block(1, "text", "Hello there.")]
    chunks, _ = chunker.chunk_page(blocks, 1, [])
    assert [c["text"] for c in chunks] == ["Hello there."]
    assert chunks[0]["blocks"] == [[1, 1]]


def test_title_opens_a_chunk_and_the_heading_path_follows_levels():
    blocks = [
        title(0, "Book", 1),
        block(1, "text", "Intro."),
        title(2, "Chapter one", 2),
        block(3, "text", "First."),
        title(4, "Section", 3),
        block(5, "text", "Deep."),
        title(6, "Chapter two", 2),
        block(7, "text", "Second."),
    ]
    chunks, stack = chunker.chunk_page(blocks, 1, [])
    assert [c["headings"] for c in chunks] == [["Book"], ["Book", "Chapter one"], ["Book", "Chapter one", "Section"], ["Book", "Chapter two"]]
    assert chunks[1]["text"] == "## Chapter one\n\nFirst."
    assert chunks[1]["blockTypes"] == ["title", "text"]
    assert stack == [(1, "Book"), (2, "Chapter two")]


def test_heading_path_carries_over_to_the_next_page():
    p1, stack = chunker.chunk_page([title(0, "Top", 1), title(1, "Part", 2), block(2, "text", "One.")], 1, [])
    p2, _ = chunker.chunk_page([block(0, "text", "Two.")], 2, stack)
    assert p2[0]["headings"] == ["Top", "Part"]
    assert p2[0]["page"] == 2


def test_text_is_packed_up_to_the_target_and_cut_at_sentence_ends():
    para = sentence_text(500)
    blocks = [block(0, "text", para), block(1, "text", para), block(2, "text", para)]
    chunks, _ = chunker.chunk_page(blocks, 1, [], target=800)
    assert len(chunks) == 3  # 500 + 2 + 500 > 800: one block per chunk
    assert all(c["chars"] <= 800 for c in chunks)
    long = block(0, "text", sentence_text(2000))
    chunks, _ = chunker.chunk_page([long], 1, [], target=800)
    assert len(chunks) == 3
    assert all(c["chars"] <= 800 for c in chunks)
    assert all(c["text"].endswith(".") for c in chunks[:-1])  # cut where a sentence ends (the last piece is the cut-off end of the test text)
    assert "".join(c["text"].replace(" ", "") for c in chunks) == long["text"].replace(" ", "")  # nothing lost


def test_boundary_exactly_at_the_target():
    a, b = "a" * 399, "b" * 399
    chunks, _ = chunker.chunk_page([block(0, "text", a), block(1, "text", b)], 1, [], target=800)
    assert len(chunks) == 1 and chunks[0]["chars"] == 800  # 399 + "\n\n" + 399 fits exactly
    chunks, _ = chunker.chunk_page([block(0, "text", a), block(1, "text", b + "b")], 1, [], target=800)
    assert len(chunks) == 2  # one more character and the second block starts a new chunk


def test_a_word_longer_than_the_target_is_still_cut():
    pieces = chunker.split_text("x" * 1000, 400)
    assert [len(p) for p in pieces] == [400, 400, 200]


def test_big_table_is_one_whole_chunk_and_never_cut():
    rows = [[f"row {i}", "x" * 40, str(i * 1000)] for i in range(30)]
    t = table(1, rows)
    assert len(t["text"]) > 800
    chunks, _ = chunker.chunk_page([block(0, "text", "Before."), t, block(2, "text", "After.")], 1, [], target=800)
    holders = [c for c in chunks if [1, 1] in c["blocks"]]
    assert len(holders) == 1 and t["text"] in holders[0]["text"]
    assert holders[0]["blockTypes"] == ["table"]  # too big to share a chunk
    assert [c["text"] for c in chunks if c is not holders[0]] == ["Before.", "After."]


def test_small_table_that_does_not_fit_moves_to_a_new_chunk_whole():
    t = table(1, [["a", "b"], ["1", "2"]])
    chunks, _ = chunker.chunk_page([block(0, "text", sentence_text(780)), t], 1, [], target=800)
    assert len(chunks) == 2 and chunks[1]["text"] == t["text"]
    chunks, _ = chunker.chunk_page([block(0, "text", "Short."), t], 1, [], target=800)
    assert len(chunks) == 1 and chunks[0]["blockTypes"] == ["text", "table"]


def test_formulas_are_not_cut():
    f = block(1, "formula", "$$x' = \\frac{x - vt}{\\sqrt{1 - v^2 / c^2}}$$")
    chunks, _ = chunker.chunk_page([block(0, "text", sentence_text(790)), f], 1, [], target=800)
    assert chunks[-1]["text"] == f["text"]


def test_merged_bbox_and_block_references():
    blocks = [block(0, "text", "One.", box=(100, 300, 500, 340)), block(1, "text", "Two.", box=(90, 350, 700, 400))]
    (chunk,), _ = chunker.chunk_page(blocks, 3, [])
    assert chunk["bbox"] == [90, 300, 700, 400]
    assert chunk["blocks"] == [[3, 0], [3, 1]]


def test_line_breaks_inside_a_block_are_squashed():
    (chunk,), _ = chunker.chunk_page([block(0, "text", "a line\nand\n\nanother  line")], 1, [])
    assert chunk["text"] == "a line and another line"


def test_chunk_document_numbers_chunks_and_carries_the_page_hash():
    pages = [{"page": 2, "blocks": [block(0, "text", "Two.")]}, {"page": 1, "blocks": [title(0, "T", 1), block(1, "text", "One.")]}]
    chunks = chunker.chunk_document("doc", pages, {1: "a" * 64, 2: "b" * 64})
    assert [c["id"] for c in chunks] == ["doc-c0001", "doc-c0002"]
    assert [c["sha256"][0] for c in chunks] == ["a", "b"]
    assert chunks[1]["headings"] == ["T"]


@pytest.mark.skipif(not (UC / "expected" / "offline.json").exists(), reason="no offline result yet")
def test_recorded_offline_result_holds_the_invariants():
    result = json.loads((UC / "expected" / "offline.json").read_text())
    for item in result["items"]:
        out = item["output"]
        assert all(c["chars"] == len(c["text"]) for c in out["chunks"])
        for b in out["blocks"]:
            if b["type"] == "table" and not b.get("dropped"):
                assert sum(1 for c in out["chunks"] if [out["page"], b["order"]] in c["blocks"] and b["text"] in c["text"]) == 1


def test_render_pdf_without_network(tmp_path):
    pdfium = pytest.importorskip("pypdfium2")
    del pdfium
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("uc04_main", UC / "python" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length 44 >>\nstream\nBT /F1 18 Tf 20 40 Td (Hello page) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf, offsets = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    pdf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    src = tmp_path / "sample.pdf"
    src.write_bytes(pdf)
    items, base = main.render_pdf(src, tmp_path, dpi=72)
    assert items == [{"id": "sample-p1", "image": "sample-p1.jpg", "document": "sample", "page": 1}]
    assert (base / "sample-p1.jpg").stat().st_size > 0
