"""Slide-PDF helpers for BabelDOC (layout boxes, Keynote inline math, list / table refinement).

1. preprocess(): Keynote-style PDFs draw inline math (STIX/CM fonts) as separate text
   objects at the END of the content stream, leaving blank gaps in the sentence.
   BabelDOC builds paragraphs in stream order, so the math becomes stray paragraphs
   and the translated sentence overlaps it.  We rewrite every text group that hosts
   inline math: remove the original glyphs (text-only redaction) and re-emit body
   text + math in reading order, so BabelDOC sees one sentence with formula chars
   inside (-> {vN} placeholders that it re-typesets inline).

2. SlideLayout: a DocLayoutModel for BabelDOC that keeps the ONNX model's figure /
   table / formula boxes but replaces its text boxes with the PDF's own text-box
   geometry (MuPDF blocks, merged into paragraphs).  The YOLO model is trained on
   papers and sometimes cuts one large slide line into two boxes.

3. refine_boxes(): split layout boxes into list items, table cells and hard-broken lines.

4. normalize_text_state() / drop_invisible_text(): input clean-ups that change nothing visible.
"""
from __future__ import annotations

import os
import re
import statistics
from pathlib import Path

import pymupdf

import fonts as F

MATH_RE = re.compile(r"STIX|^CM(MI|SY|EX|R|BX|TI|MIB|BSY)\d|CambriaMath|LatinModern.*Math|XITS|Asana", re.I)
BULLETS = set("●○■□◆◇❖•◦▪▫‣➢➤✓◼◻")


def base_font(name: str) -> str:
    return name.split("+", 1)[-1]


def is_math(span) -> bool:
    return bool(MATH_RE.search(base_font(span["font"])))


def is_bullet_span(span) -> bool:
    t = span["text"].strip()
    return len(t) == 1 and t in BULLETS


def _nonws(spans):
    return [s for s in spans if s["text"].strip()]


# ----------------------------------------------------------------------------- blocks
def page_blocks(page):
    """Text blocks with useful metadata; whitespace-only blocks are dropped."""
    out = []
    d = page.get_text("rawdict")
    for bi, b in enumerate(d["blocks"]):
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            for s in l["spans"]:
                s["text"] = "".join(c["c"] for c in s["chars"])
        lines = [l for l in b["lines"] if _nonws(l["spans"])]
        if not lines:
            continue
        spans = [s for l in lines for s in _nonws(l["spans"])]
        body = [s for s in spans if not is_math(s) and not is_bullet_span(s)]
        rect = pymupdf.Rect()
        for s in spans:
            rect |= pymupdf.Rect(s["bbox"])
        size = statistics.median([s["size"] for s in (body or spans)])
        out.append({
            "idx": bi, "rect": rect, "lines": lines, "spans": spans, "body": body,
            "size": size, "math_only": not body and all(is_math(s) or is_bullet_span(s) for s in spans),
            "bullet": any(is_bullet_span(s) for s in spans),
            "text_x0": min((s["bbox"][0] for s in body), default=rect.x0),
        })
    return out


def _separator_between(page_drawings, a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
    """True if a drawn line/rect edge separates block a (above) from b (below)."""
    y_lo, y_hi = a.y1 - 1, b.y0 + 1
    x_lo, x_hi = max(a.x0, b.x0), min(a.x1, b.x1)
    for r in page_drawings:
        for y in (r.y0, r.y1):
            if y_lo <= y <= y_hi and r.x0 <= x_hi and r.x1 >= x_lo:
                return True
    return False


def paragraph_groups(page, blocks=None):
    """Merge vertically-continuing blocks (wrapped lines stored as separate blocks)."""
    blocks = blocks if blocks is not None else page_blocks(page)
    drawings = [pymupdf.Rect(p["rect"]) for p in page.get_drawings()]
    text_blocks = [b for b in blocks if not b["math_only"]]
    text_blocks.sort(key=lambda b: (b["rect"].y0, b["rect"].x0))
    groups: list[list[dict]] = []
    for b in text_blocks:
        placed = False
        for g in groups:
            last = g[-1]
            sz = max(last["size"], b["size"])
            gap = b["rect"].y0 - last["rect"].y1
            if (
                not b["bullet"]
                and -0.3 * sz < gap < 0.9 * sz
                and abs(last["size"] - b["size"]) < 0.15 * sz
                and abs(b["text_x0"] - last["text_x0"]) < 0.8 * sz
                and not _separator_between(drawings, last["rect"], b["rect"])
            ):
                g.append(b)
                placed = True
                break
        if not placed:
            groups.append([b])
    return groups


def group_rect(g) -> pymupdf.Rect:
    r = pymupdf.Rect()
    for b in g:
        r |= b["rect"]
    return r


# ------------------------------------------------------------------------ preprocess
_font_cache: dict[str, pymupdf.Font] = {}


def _font(fname: str) -> pymupdf.Font:
    if fname not in _font_cache:
        _font_cache[fname] = (pymupdf.Font(fname[8:]) if fname.startswith("builtin:")
                              else pymupdf.Font(fontfile=fname))
    return _font_cache[fname]


def _font_chain(span) -> list[str]:
    """Font files for re-emitting a span: the closest family available on this platform, then
    broad fallbacks (Noto / Source Han from BabelDOC's cache exist everywhere)."""
    name = base_font(span["font"]).lower()
    bold = "bold" in name or bool(span["flags"] & 16)
    italic = "italic" in name or "oblique" in name or bool(span["flags"] & 2)
    k = (2 if bold else 0) + (1 if italic else 0)  # 0 reg, 1 it, 2 bd, 3 bdit
    if is_math(span):
        # renamed math fonts: BabelDOC treats '*Math' fonts as formula -> glyphs kept verbatim
        chain = F.math_chain(italic)
    else:
        if "gill" in name:
            fam = "gill"
        elif any(m in name for m in ("courier", "menlo", "consol", "mono", "monaco")):
            fam = "mono"
        elif "calibri" in name:
            fam = "calibri"
        elif any(m in name for m in ("gulim", "dotum", "batang", "malgun", "nanum", "sdgothic", "gothic")):
            fam = "korean"
        elif any(m in name for m in ("times", "palatino", "georgia", "serif")):
            fam = "serif"
        else:
            fam = "sans"
        chain = F.family_chain(fam, k)
    return chain or ["builtin:helv"]


def _runs(text: str, chain: list[str]):
    """Split text into (font_file, substring) runs using per-char glyph fallback."""
    runs = []
    for ch in text:
        f = next((f for f in chain if _font(f).has_glyph(ord(ch))), chain[-1])
        if runs and runs[-1][0] == f:
            runs[-1][1] += ch
        else:
            runs.append([f, ch])
    return runs


def _rgb(c: int):
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


def hidden_words(page):
    """Words MuPDF drops by default (fully clipped or outside the page) but parsers like BabelDOC still see."""
    seen = {(round(w[0]), round(w[1]), w[4]) for w in page.get_text("words")}
    flags = pymupdf.TEXTFLAGS_WORDS & ~pymupdf.TEXT_MEDIABOX_CLIP
    return [w for w in page.get_text("words", flags=flags) if (round(w[0]), round(w[1]), w[4]) not in seen]


def preprocess(src: str, dst: str) -> dict:
    doc = pymupdf.open(src)
    report = {"pages": []}
    for page in doc:
        blocks = page_blocks(page)
        # host lines = lines with body text
        host_lines = []
        for b in blocks:
            if b["math_only"]:
                continue
            for l in b["lines"]:
                if any(not is_math(s) and not is_bullet_span(s) for s in _nonws(l["spans"])):
                    host_lines.append((b, l))
        assigned: dict[int, list] = {}  # id(line) -> math spans
        line_owner: dict[int, tuple] = {}
        moved = 0
        for b in blocks:
            for l in b["lines"]:
                for s in _nonws(l["spans"]):
                    if not is_math(s) or (not b["math_only"] and not all(is_math(x) for x in _nonws(l["spans"]))):
                        continue
                    r = pymupdf.Rect(s["bbox"])
                    cy = (r.y0 + r.y1) / 2
                    best, best_ov = None, 0.0
                    for hb, hl in host_lines:
                        if hb is b:
                            continue
                        lr = pymupdf.Rect(hl["bbox"])
                        h = lr.height
                        if not (lr.y0 - 0.3 * h <= cy <= lr.y1 + 0.3 * h):
                            continue
                        br = hb["rect"]
                        if not (min(lr.x0, br.x0) - 1.0 * h <= r.x0 and r.x1 <= max(lr.x1, br.x1) + 1.0 * h):
                            continue
                        ov = min(r.y1, lr.y1) - max(r.y0, lr.y0)
                        if ov > best_ov:
                            best, best_ov = (hb, hl), ov
                    if best:
                        assigned.setdefault(id(best[1]), []).append(s)
                        line_owner[id(best[1])] = best
                        moved += 1
        hidden = hidden_words(page)
        if not assigned and not hidden:
            report["pages"].append({"page": page.number + 1, "inline_math": 0, "groups": 0, "hidden_words": 0})
            continue
        dirty_blocks = {id(hb) for hb, _ in line_owner.values()}
        groups = paragraph_groups(page, blocks)
        if hidden:
            # clipped/invisible text exists: rewrite every visible text group so the
            # hidden glyphs can be removed without touching what the reader sees
            dirty_groups = groups
            for w in hidden:
                r = pymupdf.Rect(w[:4])
                dy = r.height * 0.2
                page.add_redact_annot(pymupdf.Rect(r.x0, r.y0 + dy, r.x1, r.y1 - dy), fill=False)
        else:
            dirty_groups = [g for g in groups if any(id(b) in dirty_blocks for b in g)]
        # 1) remove original glyphs (text only)
        for g in dirty_groups:
            for b in g:
                for l in b["lines"]:
                    for s in _nonws(l["spans"]) + assigned.get(id(l), []):
                        r = pymupdf.Rect(s["bbox"])
                        dy = r.height * 0.2
                        page.add_redact_annot(pymupdf.Rect(r.x0, r.y0 + dy, r.x1, r.y1 - dy), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        # 2) re-emit in reading order: group -> visual row -> x
        for g in sorted(dirty_groups, key=lambda g: (group_rect(g).y0, group_rect(g).x0)):
            items = []
            for b in g:
                for l in b["lines"]:
                    for s in _nonws(l["spans"]) + assigned.get(id(l), []):
                        items.append(s)
            # rows by baseline proximity
            items.sort(key=lambda s: s["origin"][1])
            rows: list[list] = []
            for s in items:
                if rows and abs(s["origin"][1] - rows[-1][0]["origin"][1]) < 0.6 * s["size"]:
                    rows[-1].append(s)
                else:
                    rows.append([s])
            for row in rows:
                # split spans into words placed at their original glyph origins, then
                # interleave body words and math by x so the stream follows reading order
                tokens = []
                for s in row:
                    cur = None
                    for c in s["chars"]:
                        if cur is None or (cur["text"] and cur["text"][-1] == " " and c["c"] != " "):
                            cur = {"x": c["bbox"][0], "origin": c["origin"], "text": "", "span": s}
                            tokens.append(cur)
                        cur["text"] += c["c"]
                for t in sorted(tokens, key=lambda t: t["x"]):
                    s = t["span"]
                    tw = pymupdf.TextWriter(page.rect)
                    pos = pymupdf.Point(t["origin"])
                    for fname, chunk in _runs(t["text"], _font_chain(s)):
                        _, pos = tw.append(pos, chunk, font=_font(fname), fontsize=s["size"])
                    tw.write_text(page, color=_rgb(s["color"]))
        report["pages"].append({"page": page.number + 1, "inline_math": moved, "groups": len(dirty_groups),
                                "hidden_words": len(hidden)})
    doc.save(dst, garbage=3, deflate=True)
    return report


# --------------------------------------------------------------------- layout model
def make_slide_layout(base_model):
    import numpy as np
    from babeldoc.docvision.base_doclayout import DocLayoutModel, YoloBox, YoloResult

    names = base_model._names
    keep_cls = {k for k, v in names.items() if v in ("figure", "table", "isolate_formula")}
    cls_text = next(k for k, v in names.items() if v == "plain text")
    cls_title = next(k for k, v in names.items() if v == "title")

    class SlideLayout(DocLayoutModel):
        @property
        def stride(self):
            return base_model.stride

        def handle_document(self, pages, mupdf_doc, translate_config, save_debug_image):
            for page, res in base_model.handle_document(pages, mupdf_doc, translate_config, save_debug_image):
                mp = mupdf_doc[page.page_number]
                groups = paragraph_groups(mp)
                rects = [group_rect(g) for g in groups]
                sizes = [max(b["size"] for b in g) for g in groups]
                max_size = max(sizes, default=0)
                boxes = []
                for r, sz in zip(rects, sizes):
                    is_title = sz >= max_size * 0.98 and r.y0 < mp.rect.height * 0.3 and sz > 1.2 * statistics.median(sizes)
                    boxes.append(YoloBox(
                        xyxy=np.array([r.x0 - 1, r.y0 - 1, r.x1 + 1, r.y1 + 1], dtype=np.float32),
                        conf=np.float32(0.99),
                        cls=np.float32(cls_title if is_title else cls_text)))
                for yb in res.boxes:
                    if int(yb.cls) not in keep_cls:
                        continue
                    br = pymupdf.Rect(*[float(v) for v in yb.xyxy[:4]])
                    overlap = any((br & r).get_area() > 0.3 * max(r.get_area(), 1) for r in rects)
                    if not overlap:
                        boxes.append(yb)
                yield page, YoloResult(names=names, boxes=boxes)

    layout = SlideLayout()
    layout._names = names
    return layout


# ------------------------------------------------------------------- box refinement
MARKER_RE = re.compile(r"^\s*((?:[a-zA-Z]|\d{1,2}|[ivxIVX]{1,4})[.)]|\(\w{1,3}\)|[①-⑳])")


def _page_lines(page):
    raw = page.get_text("rawdict")
    lines = []
    for b in raw["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            chars = [c for s in l["spans"] for c in s["chars"]]
            if any(c["c"].strip() for c in chars):
                lines.append({"rect": pymupdf.Rect(l["bbox"]), "chars": chars})
    return lines


def _rows(lines):
    """Visual text rows (lines merged when on the same baseline), chars sorted by x."""
    lines = sorted(lines, key=lambda l: (l["rect"].y0, l["rect"].x0))
    rows = []
    for l in lines:
        for r in rows:
            ov = min(r["rect"].y1, l["rect"].y1) - max(r["rect"].y0, l["rect"].y0)
            if ov > 0.5 * min(r["rect"].height, l["rect"].height):
                r["chars"] += l["chars"]
                r["rect"] |= l["rect"]
                break
        else:
            rows.append({"rect": pymupdf.Rect(l["rect"]), "chars": list(l["chars"])})
    for r in rows:
        r["chars"].sort(key=lambda c: c["bbox"][0])
        r["text"] = "".join(c["c"] for c in r["chars"])
    return rows


def _marker_len(row) -> int:
    """Number of leading chars that form a list marker (bullet glyph or 'a.', '1)', '(i)')."""
    text = row["text"]
    stripped = text.lstrip()
    lead = len(text) - len(stripped)
    if stripped and stripped[0] in BULLETS:
        return lead + 1
    m = MARKER_RE.match(text)
    if not m:
        return 0
    n = len(m.group(0))
    if n >= len(text):
        return 0
    nxt, last = row["chars"][n], row["chars"][n - 1]
    gap = nxt["bbox"][0] - last["bbox"][2]
    h = last["bbox"][3] - last["bbox"][1]
    # a marker must be followed by whitespace or a visible gap ('e.g.' is not a marker)
    return n if (not nxt["c"].strip() or gap > 0.25 * h) else 0


def _chars_rect(chars) -> pymupdf.Rect:
    r = pymupdf.Rect()
    for c in chars:
        if c["c"].strip():
            r |= pymupdf.Rect(c["bbox"])
    return r


def _table_cells(page):
    """Drawn rectangles that belong to a grid: another rect shares their row edges and
    another shares their column edges (a lone background panel does not qualify)."""
    rects = [pymupdf.Rect(p["rect"]) for p in page.get_drawings()
             if p["rect"].width > 4 and p["rect"].height > 4]
    cells = []
    for c in rects:
        same_row = any(o is not c and abs(o.y0 - c.y0) < 1.5 and abs(o.y1 - c.y1) < 1.5 and abs(o.x0 - c.x0) > 1
                       for o in rects)
        same_col = any(o is not c and abs(o.x0 - c.x0) < 1.5 and abs(o.x1 - c.x1) < 1.5 and abs(o.y0 - c.y0) > 1
                       for o in rects)
        if same_row and same_col:
            cells.append(c)
    return cells


def _cell_rect(box: pymupdf.Rect, cells, page_rect) -> pymupdf.Rect | None:
    """Smallest table cell that encloses the box."""
    best = None
    for c in cells:
        if not c.contains(pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)):
            continue
        if c.width < box.width * 0.95 or c.height < box.height * 0.95:
            continue
        if c.height > 2.5 * box.height or c.get_area() > 0.25 * page_rect.get_area():
            continue
        if best is None or c.get_area() < best.get_area():
            best = c
    return best


# Google Slides draws faux bold as fill+stroke text: "<rgb> rg <rgb> RG <w> w 2 J 0 j 10 M [] 0 d BT 2 Tr".
# Inside one paragraph the bold glyphs then carry stroke state the spaces lack, BabelDOC finds no
# common style and typesets the whole translation in black.  Dropping the stroke keeps the colour.
FAUX_BOLD = re.compile(rb"((?:-?[\d.]+\s+){3}rg\s+)(?:-?[\d.]+\s+){3}RG\s+-?[\d.]+\s+w\s+\d\s+J\s+\d\s+j\s+"
                       rb"-?[\d.]+\s+M\s+\[[^\]]*\]\s*-?[\d.]+\s+d\s+(BT\s+)2(\s+Tr)")


def normalize_text_state(src: str, dst: str) -> int:
    """Copy of the PDF with faux-bold stroke state removed from text (BabelDOC input only)."""
    doc = pymupdf.open(src)
    n = 0
    for page in doc:
        for x in page.get_contents():
            s = doc.xref_stream(x)
            s2, k = FAUX_BOLD.subn(rb"\1\g<2>0\3", s)
            if k:
                doc.update_stream(x, s2)
                n += k
    doc.save(dst, garbage=1, deflate=True)
    return n


def drop_invisible_text(src: str, dst: str) -> int:
    """Copy of the PDF without invisible text (render mode 3) - typically the OCR layer of a
    scanned page.  BabelDOC would otherwise typeset its translation visibly on top of the scan;
    the scan itself is translated through the image path instead."""
    doc = pymupdf.open(src)
    n = 0
    for page in doc:
        boxes = [pymupdf.Rect(s["bbox"]) for s in page.get_texttrace() if s.get("type") == 3]
        if not boxes:
            continue
        for r in boxes:
            dy = r.height * 0.2
            page.add_redact_annot(pymupdf.Rect(r.x0, r.y0 + dy, r.x1, r.y1 - dy), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        n += len(boxes)
    doc.save(dst, garbage=1, deflate=True)
    return n


def count_invisible_text(path: str) -> int:
    doc = pymupdf.open(path)
    return sum(1 for page in doc for s in page.get_texttrace() if s.get("type") == 3)


SENTENCE_END = re.compile(r"[.!?:;。：！？]\s*$")
ABBREV_END = re.compile(r"\b(?:e\.g|i\.e|etc|vs|Fig|Figs|Eq|Eqs|al|cf|No|Dr|Mr|Ms|Prof|Sec|Ch)\.\s*$", re.I)


def _first_word_width(row) -> float:
    chars = row["chars"]
    i = 0
    while i < len(chars) and not chars[i]["c"].strip():
        i += 1
    j = i
    while j < len(chars) and chars[j]["c"].strip():
        j += 1
    return chars[j - 1]["bbox"][2] - chars[i]["bbox"][0] if j > i else 0.0


def _hard_break(prev, cur, rows_x1: float, rows_w: float, sentences: bool) -> bool:
    """True when `cur` cannot continue the wrapped paragraph of `prev`: cur's first word would
    have fitted on prev's line (with a 1 em margin, inside the widest row of the block) so the
    break was typed, not wrapped; or (slides) prev ends a sentence / 'Label:' line, or prev is an
    indented, not-full line (a formula / schema line) and cur returns to the left margin."""
    em = prev["rect"].height
    if cur["text"].lstrip()[:1] in set(".,;:!?)]}。，、；：！？）"):
        return False  # a row starting with punctuation continues the sentence above
    if prev["rect"].x1 + 0.3 * em + _first_word_width(cur) + em < rows_x1:
        return True
    if not sentences:
        return False
    t = prev["text"].strip()
    if SENTENCE_END.search(t) and not ABBREV_END.search(t):
        return True
    h = prev["rect"].height
    return prev["rect"].x0 > cur["rect"].x0 + 1.5 * h and prev["rect"].x1 < rows_x1 - 0.1 * rows_w


def refine_boxes(page, boxes, names, slide_breaks: bool = False):
    """Split list items / bullets into marker + text boxes (hanging indent), split text boxes at
    hard line breaks, and widen table-cell text boxes to their cell so short translations are
    not shrunk.  slide_breaks: also break after rows ending a sentence (slides, not prose)."""
    import numpy as np
    from babeldoc.docvision.base_doclayout import YoloBox

    text_cls = {k for k, v in names.items() if v in ("plain text", "title")}
    lines = _page_lines(page)
    cells = _table_cells(page)
    out = []

    def mk(rect, cls):
        return YoloBox(xyxy=np.array([rect.x0, rect.y0, rect.x1, rect.y1], dtype=np.float32),
                       conf=np.float32(0.99), cls=np.float32(cls))

    table_cls = {k for k, v in names.items() if v == "table"}
    for yb in boxes:
        cls = int(yb.cls)
        if cls in table_cls and cells:
            # one text box per non-empty cell, so each translation can use its whole cell
            T = pymupdf.Rect(*[float(v) for v in yb.xyxy[:4]])
            mine = [c for c in cells if (c & T).get_area() > 0.8 * c.get_area()]
            # keep the innermost rects only (a row band may also be drawn as a rect)
            mine = [c for c in mine if not any(o is not c and c.contains(o) for o in mine)]
            used = False
            for c in mine:
                if any(c.contains(pymupdf.Point((l["rect"].x0 + l["rect"].x1) / 2, (l["rect"].y0 + l["rect"].y1) / 2))
                       for l in lines):
                    out.append(mk(c + (1.5, 1.5, -1.5, -1.5), min(text_cls - {k for k, v in names.items() if v == "title"})))
                    used = True
            if not used:
                out.append(yb)
            continue
        if cls not in text_cls:
            out.append(yb)
            continue
        R = pymupdf.Rect(*[float(v) for v in yb.xyxy[:4]])
        inside = _rows([l for l in lines
                        if R.contains(pymupdf.Point((l["rect"].x0 + l["rect"].x1) / 2,
                                                    (l["rect"].y0 + l["rect"].y1) / 2))])
        # group rows into items: a row with a marker starts a new item.  Bullet glyphs
        # always do; 'a.' / '(1)' only when they cannot be a wrapped sentence that
        # happens to start with "(1)": first row, previous row visibly short, or the
        # previous row itself began with such a marker.
        items = []
        prev = None
        rows_x1 = max((r["rect"].x1 for r in inside), default=R.x1)
        rows_w = rows_x1 - min((r["rect"].x0 for r in inside), default=R.x0)
        for r in inside:
            n = _marker_len(r)
            if n and r["text"].lstrip()[:1] not in BULLETS and prev is not None:
                prev_short = prev["rect"].x1 < R.x1 - 0.15 * R.width
                if not (prev_short or prev.get("_marker")):
                    n = 0
            r["_marker"] = bool(n)
            # unmarked lines after a hard break (e.g. "ID = {...}" lines under a sentence)
            # become their own paragraphs instead of being reflowed into one block
            brk = prev is not None and _hard_break(prev, r, rows_x1, rows_w, slide_breaks)
            # first-line indent starts a new paragraph (BabelDOC did this itself inside one big
            # box); not for the continuation of a marker row, which is indented to the text
            if prev is not None and not brk and r["rect"].x0 > prev["rect"].x0 + 1.0 * prev["rect"].height:
                head = items[-1]["rows"][0] if items else None
                marker_head = head is not None and len(items[-1]["rows"]) == 1 and (
                    items[-1]["marker_n"] or MARKER_RE.match(head["text"]) or head["text"].lstrip()[:1] in BULLETS)
                brk = not marker_head
            if n or not items or brk:
                items.append({"marker_n": n, "rows": [r]})
            else:
                items[-1]["rows"].append(r)
            prev = r
        if len(items) <= 1 and not any(it["marker_n"] for it in items):
            cell = _cell_rect(R, cells, page.rect)
            out.append(mk(cell + (1.5, 1.5, -1.5, -1.5) if cell else R, cls))
            continue
        # Split markers off only when every item in this box is a single row.  For
        # multi-row items the first line starts ~1pt right of the others (glyph
        # bearings) and BabelDOC then applies a 2-character CJK first-line indent;
        # keeping the marker in the paragraph avoids that (bullets are kept verbatim
        # as formula chars).  One rule per box keeps sibling items looking alike.
        split_markers = all(len(it["rows"]) == 1 for it in items if it["marker_n"])
        for i, it in enumerate(items):
            first = it["rows"][0]
            n = it["marker_n"] if split_markers else 0
            if n:
                marker_chars = first["chars"][:n]
                rest = [c for c in first["chars"][n:] if c["c"].strip()]
                mr = _chars_rect(marker_chars) + (-0.5, -0.5, 0.5, 0.5)
                if rest:
                    # BabelDOC grows layout boxes sideways until they touch a neighbour;
                    # let the marker box reach the text so the text box cannot grow left
                    # (a box wider than its first line is read as a first-line indent)
                    mr.x1 = max(mr.x1, rest[0]["bbox"][0] - 0.3)
                out.append(mk(mr, cls))
                body_chars = rest + [c for r in it["rows"][1:] for c in r["chars"]]
                if not body_chars:
                    continue
                # hanging indent = text start of the first row, but never cut off a
                # continuation row that starts further left
                cont = [c for r in it["rows"][1:] for c in r["chars"] if c["c"].strip()]
                x0 = min([c["bbox"][0] for c in rest[:1]] + [c["bbox"][0] for c in cont]) \
                    if rest else _chars_rect(body_chars).x0
            else:
                body_chars = [c for r in it["rows"] for c in r["chars"]]
                x0 = _chars_rect(body_chars).x0
            br = _chars_rect(body_chars)
            y1 = br.y1
            if i + 1 < len(items):  # give the text room down to the next item
                y1 = max(y1, items[i + 1]["rows"][0]["rect"].y0 - 0.5)
            out.append(mk(pymupdf.Rect(x0 - 0.5, br.y0 - 0.5, max(R.x1, br.x1), y1), cls))
    return out


def _draw_boxes(page, before, after, out: Path):
    """Debug image: model boxes in red, refined boxes in green."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = pymupdf.open()
    tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    p = tmp[0]
    for boxes, color, w in ((before, (1, 0, 0), 1.5), (after, (0, 0.7, 0), 1)):
        for b in boxes:
            x0, y0, x1, y1 = (float(v) for v in b.xyxy[:4])
            p.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=color, width=w)
    zoom = 1200 / p.rect.width
    p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(out)


def make_refined_layout(base_model, slide_breaks: bool = False):
    from babeldoc.docvision.base_doclayout import DocLayoutModel, YoloResult

    names = base_model._names

    class RefinedLayout(DocLayoutModel):
        @property
        def stride(self):
            return base_model.stride

        def handle_document(self, pages, mupdf_doc, translate_config, save_debug_image):
            for page, res in base_model.handle_document(pages, mupdf_doc, translate_config, save_debug_image):
                mp = mupdf_doc[page.page_number]
                boxes = refine_boxes(mp, res.boxes, names, slide_breaks)
                dbg = os.environ.get("LT_BOX_DEBUG")
                if dbg:
                    _draw_boxes(mp, res.boxes, boxes, Path(dbg) / f"boxes_p{page.page_number + 1}.png")
                yield page, YoloResult(names=names, boxes=boxes)

    layout = RefinedLayout()
    layout._names = names
    return layout


if __name__ == "__main__":
    import json
    import sys

    rep = preprocess(sys.argv[1], sys.argv[2])
    print(json.dumps([p for p in rep["pages"] if p["inline_math"] or p["hidden_words"]], ensure_ascii=False))
