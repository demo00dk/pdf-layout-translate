"""Translate the text inside raster images (screenshots, figures, scanned pages) of a PDF.

Companion of bd_two_pass.py (BabelDOC leaves images untouched):
  record : OCRs every content image of the source PDF -> <work>/img/ (PNGs + ocr.json),
           groups lines into paragraphs and writes img_segments.json / img_translations.json
           (carried over by source text) / img_todo.json
  apply  : erases the translated paragraphs inside each image, draws the Chinese text in the
           original colour, swaps the images inside BabelDOC's mono PDF and rebuilds the
           side-by-side dual PDF from the original + new mono.
A translation equal to the source (code, data, names...) means "leave this text alone";
an empty translation erases the text.

OCR runs locally: RapidOCR (onnxruntime) with PaddleOCR PP-OCRv5 models.  The recognition model
follows the source language (Korean; Chinese/English/Japanese; Latin; Cyrillic; ...) and reads
the Latin text mixed into it.  Models are downloaded once into <runtime>/models.

usage:
  python image_translate.py record <pdf> <work> [--lang ko]
  python image_translate.py preview <pdf> <work>          # edited images -> <work>/img/preview/
  python image_translate.py apply <pdf> <mono.pdf> <work> [--dual <dual.pdf>]
  python image_translate.py warmup [--lang en,ko]         # download the OCR models
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path

import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

import fonts as F
import lt_paths as P

MIN_PAGE_FRAC = 0.02   # skip images smaller than 2% of the page (logos, icons)
MIN_SIDE_PX = 40
HANGUL = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]")
HAN_KANA = re.compile(r"^[\u3040-\u30ff\u4e00-\u9fff\s]+$")
CODE_RE = re.compile(r"[=#|{};<>\\$@^~`]|::|--|\w_\w|\w\.\w+\(")
URL_RE = re.compile(r"^\s*(https?://)?[\w.-]+\.(org|com|net|io|edu|gov|kr|cn|ac\.kr)(/\S*)?\s*$", re.I)

# --------------------------------------------------------------------------- images
def parse_pages(spec: str | None) -> set[int] | None:
    """'1-5,8' -> {0,1,2,3,4,7} (0-based); None = all pages."""
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a) - 1, int(b)))
        elif part:
            out.add(int(part) - 1)
    return out


def content_images(doc: fitz.Document, pages: set[int] | None = None) -> dict[int, list[dict]]:
    """xref -> placements [{page, bbox}] for images big enough to carry readable text."""
    out: dict[int, list[dict]] = {}
    for page in doc:
        if pages is not None and page.number not in pages:
            continue
        area = page.rect.width * page.rect.height
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref", 0)
            b = fitz.Rect(info["bbox"]) & page.rect
            if not xref or b.is_empty:
                continue
            if b.width * b.height < MIN_PAGE_FRAC * area or min(info["width"], info["height"]) < MIN_SIDE_PX:
                continue
            out.setdefault(xref, []).append({"page": page.number, "bbox": [round(v, 2) for v in b]})
    return out


def load_rgba(doc: fitz.Document, xref: int) -> np.ndarray:
    """Decoded image as HxWx4 uint8 RGBA (soft mask applied as alpha)."""
    pix = fitz.Pixmap(doc, xref)
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
    alpha = np.full(rgb.shape[:2], 255, np.uint8)
    smask = doc.xref_get_key(xref, "SMask")
    if smask[0] == "xref":
        m = fitz.Pixmap(doc, int(smask[1].split()[0]))
        a = np.frombuffer(m.samples, np.uint8).reshape(m.h, m.w, m.n)[:, :, 0]
        if a.shape != alpha.shape:
            a = cv2.resize(a, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)
        alpha = a.copy()
    return np.dstack([rgb, alpha])


def flatten(rgba: np.ndarray) -> np.ndarray:
    """RGBA -> RGB composited on white (what a reader sees on a white page)."""
    a = rgba[:, :, 3:4].astype(np.float32) / 255.0
    return (rgba[:, :, :3].astype(np.float32) * a + 255.0 * (1.0 - a)).round().astype(np.uint8)


def save_png(path: Path, rgb: np.ndarray):
    code = cv2.COLOR_RGBA2BGRA if rgb.shape[2] == 4 else cv2.COLOR_RGB2BGR
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb, code))
    assert ok
    path.write_bytes(buf.tobytes())


def read_png(path: Path) -> np.ndarray:
    return cv2.cvtColor(cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)




# --------------------------------------------------------------------------- OCR
# source language -> PP-OCRv5 recognition model (RapidOCR LangRec value); Latin-script languages
# not listed use "latin"
REC_LANG = {"ko": "korean", "zh": "ch", "ja": "ch", "en": "ch",
            "ru": "eslav", "uk": "eslav", "be": "eslav",
            "bg": "cyrillic", "sr": "cyrillic", "mk": "cyrillic", "kk": "cyrillic", "mn": "cyrillic",
            "th": "th", "el": "el", "ar": "arabic", "fa": "arabic", "ur": "arabic",
            "hi": "devanagari", "mr": "devanagari", "ne": "devanagari", "ta": "ta", "te": "te"}
_ENGINES: dict = {}


def rec_lang(lang: str) -> str:
    return REC_LANG.get(lang.split("-")[0].lower(), "latin")


def engine(lang: str):
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    key = rec_lang(lang)
    if key not in _ENGINES:
        P.MODELS.mkdir(parents=True, exist_ok=True)
        # server detection model: separates stacked table cells in console screenshots that the
        # mobile one merges into one garbled column; it splits sentences into words instead,
        # which merge_rows() puts back together
        _ENGINES[key] = RapidOCR(params={
            "Global.model_root_dir": str(P.MODELS), "Global.log_level": "error", "Global.use_cls": False,
            "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.SERVER,
            "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
            "Rec.lang_type": LangRec(key)})
    return _ENGINES[key]


def warmup(langs: list[str]):
    """Download the models for these languages (setup)."""
    for lang in langs:
        engine(lang)(np.full((64, 256, 3), 255, np.uint8))
        print(f"[ocr] models ready for '{lang}' ({rec_lang(lang)})", flush=True)


def _is_han(ch: str) -> bool:
    return bool(ch) and ("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")


def _split_pipes(line: dict, words) -> list[dict]:
    """psql / console tables: one OCR line spans several cells separated by '|'.  Cut it into one
    line per cell, placing characters by interpolating inside each word box."""
    if "|" not in line["text"] or not words:
        return [line]
    chars = []  # (char, x0, x1, y0, y1)
    for w in words:
        t, quad = w[0], w[2]
        xs, ys = [q[0] for q in quad], [q[1] for q in quad]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        n = max(1, len(t))
        if chars and t and not (_is_han(chars[-1][0]) and _is_han(t[0])):
            chars.append((" ", x0, x0, y0, y1))
        for i, ch in enumerate(t):
            chars.append((ch, x0 + (x1 - x0) * i / n, x0 + (x1 - x0) * (i + 1) / n, y0, y1))
    runs, cur = [], []
    for c in chars:
        if c[0] == "|":
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append(c)
    if cur:
        runs.append(cur)
    out = []
    for r in runs:
        vis = [c for c in r if c[0].strip()]
        if vis:
            out.append({**line, "text": re.sub(r"\s+", " ", "".join(c[0] for c in r)).strip(),
                        "box": [min(c[1] for c in vis), min(c[3] for c in vis),
                                max(c[2] for c in vis), max(c[4] for c in vis)]})
    return out


def merge_rows(lines: list[dict]) -> list[dict]:
    """Join word boxes that sit on one row with at most a word gap between them (table cells and
    columns are further apart).  Cells cut at '|' stay apart."""
    lines = sorted(lines, key=lambda l: l["box"][0])
    out: list[dict] = []
    for L in lines:
        h = L["box"][3] - L["box"][1]
        for M in reversed(out):
            mh = M["box"][3] - M["box"][1]
            ov = min(M["box"][3], L["box"][3]) - max(M["box"][1], L["box"][1])
            gap = L["box"][0] - M["box"][2]
            # a word space is < 0.45 em; table columns (Explorer, psql) sit further apart.
            # Detection boxes are padded, so neighbouring words may overlap a little.
            if (ov >= 0.6 * min(h, mh) and -0.6 * h < gap <= 0.45 * min(h, mh)
                    and not (L.get("cell") or M.get("cell")) and 0.6 < h / max(mh, 1) < 1.6):
                tight = 0 <= gap <= 0.2 * min(h, mh)
                sep = "" if tight or (_is_han(M["text"][-1:]) and _is_han(L["text"][:1])) else " "
                left, text = M["text"], L["text"]
                if gap < 0 and text:
                    # overlapping boxes: the glyphs in the overlap are read by both crops
                    # ("Please select t" + "the installation"; "installation" + "n folder").
                    # Drop them from the left part; a space before them means they began a word.
                    cw = (L["box"][2] - L["box"][0]) / max(1, len(text))
                    n = int(round(-gap / max(cw, 1e-6)))
                    for k in range(min(n + 1, 4, len(text)), 0, -1):
                        dup = text[:k]
                        if dup.strip() and left.endswith(dup):
                            core = left[:-k]
                            left, sep = (core.rstrip(), " ") if core.endswith(" ") else (core, "")
                            break
                M["text"] = left + sep + text
                M["box"] = [M["box"][0], min(M["box"][1], L["box"][1]), max(M["box"][2], L["box"][2]),
                            max(M["box"][3], L["box"][3])]
                M["score"] = min(M["score"], L["score"])
                break
        else:
            out.append(dict(L))
    return out


def reread_latin(img: np.ndarray, lines: list[dict], lang: str) -> None:
    """Korean model on Latin-only lines drops spaces ("wesplit"): re-read those crops with the
    Chinese/English model and keep whichever reading is more confident."""
    if rec_lang(lang) not in ("korean",):
        return
    other = None
    H, W = img.shape[:2]
    for L in lines:
        if HANGUL.search(L["text"]) or not re.search(r"[A-Za-z]{2}", L["text"]):
            continue
        x0, y0, x1, y1 = (int(v) for v in L["box"])
        crop = img[max(0, y0 - 2):min(H, y1 + 2), max(0, x0 - 2):min(W, x1 + 2)]
        if crop.size == 0:
            continue
        other = other or engine("en")
        try:
            r = other(crop, use_det=False, use_cls=False, use_rec=True)
        except Exception:  # noqa: BLE001
            return
        if r.txts and r.scores and float(r.scores[0]) >= L["score"] - 0.02:
            L["text"], L["score"] = str(r.txts[0]).strip(), round(float(r.scores[0]), 3)


def run_ocr(paths: list[Path], lang: str) -> dict[str, list[dict]]:
    """image path -> [{box: [x0, y0, x1, y1], text, score}]"""
    eng = engine(lang)
    res = {}
    for p in paths:
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        r = eng(img, return_word_box=True)
        boxes = list(r.boxes) if r.boxes is not None else []
        words = list(r.word_results) if r.word_results else [None] * len(boxes)
        lines = []
        for box, text, score, wd in zip(boxes, r.txts or [], r.scores or [], words):
            xs, ys = [float(pt[0]) for pt in box], [float(pt[1]) for pt in box]
            line = {"box": [min(xs), min(ys), max(xs), max(ys)], "text": str(text).strip(),
                    "score": round(float(score), 3)}
            parts = _split_pipes(line, wd)
            if len(parts) > 1:
                for q in parts:
                    q["cell"] = True
            lines.extend(parts)
        lines = merge_rows(lines)
        reread_latin(img, lines, lang)
        res[str(p)] = lines
    return res


# --------------------------------------------------------------------------- lines -> paragraphs
def keep_line(l: dict, lang: str) -> bool:
    t = l["text"].strip()
    if l.get("score", 1) < 0.5:
        return False
    if sum(ch.isalnum() for ch in t) < 2:
        return False          # icons read as "山", "×", "□", single letters
    if lang not in ("zh", "ja") and HAN_KANA.match(t) and len(t.replace(" ", "")) <= 2:
        return False
    return True


def is_prose(t: str) -> bool:
    chars = [c for c in t if not c.isspace()]
    if not chars or CODE_RE.search(t):
        return False
    return sum(c.isalpha() for c in chars) / len(chars) >= 0.6


def line_zones(lines: list[dict]) -> list[tuple[float, float]]:
    """Vertical zone each line may claim: up to halfway to the nearest line above / below that
    overlaps it horizontally (tight console / list text), else 0.4 x its height beyond the box."""
    zones = []
    for L in lines:
        x0, y0, x1, y1 = L["box"]
        h, cy = y1 - y0, (y0 + y1) / 2
        top, bot = y0 - 0.4 * h, y1 + 0.4 * h
        for M in lines:
            if M is L or min(x1, M["box"][2]) - max(x0, M["box"][0]) <= 0:
                continue
            cm = (M["box"][1] + M["box"][3]) / 2
            if cm < cy:
                top = max(top, (cm + cy) / 2 if M["box"][3] > y0 - 0.4 * h else top)
            elif cm > cy:
                bot = min(bot, (cm + cy) / 2 if M["box"][1] < y1 + 0.4 * h else bot)
        zones.append((top, bot))
    return zones


def em_of(text: str, full_h: float, dense_h: float) -> float:
    """Font size (em, px) of a line from its ink height and its dense band (x-height for Latin)."""
    letters = [c for c in text if c.isalpha()]
    # Hangul / CJK glyphs fill ~0.9 em; once they are a quarter of the letters, the dense band is
    # their height rather than a Latin x-height
    if letters and sum(ord(c) > 0x2E80 for c in letters) / len(letters) > 0.25:
        return 0.5 * (full_h / 0.92 + dense_h / 0.80)
    caps = sum(c.isupper() for c in letters) / max(1, len(letters))
    digits = sum(c.isdigit() for c in text) / max(1, len(text.replace(" ", "")))
    xh = 0.70 if caps > 0.6 or digits > 0.5 else 0.53        # dense band: cap height or x-height
    asc = caps > 0 or any(c in "bdfhklt0123456789" for c in text)
    desc = any(c in "gjpqy" for c in text)
    full_ratio = 0.53 + (0.20 if asc else 0) + (0.21 if desc else 0)
    if caps > 0.6:
        full_ratio = 0.70 + (0.21 if desc else 0)
    return 0.5 * (full_h / full_ratio + dense_h / xh)


def line_style(flat: np.ndarray, alpha: np.ndarray, box: list[float], text: str,
               zone: tuple[float, float]) -> dict | None:
    """Background / text colour, ink extent, font size and stroke weight of one OCR line."""
    H, W = flat.shape[:2]
    x0, y0, x1, y1 = box
    cx0, cx1 = max(0, int(math.floor(x0))), min(W, int(math.ceil(x1)))
    cy0, cy1 = max(0, int(math.floor(y0))), min(H, int(math.ceil(y1)))
    Z0, Z1 = max(0, int(math.floor(zone[0]))), min(H, int(math.ceil(zone[1])))
    # padded detection boxes can reach into the neighbouring lines of tight console text: only
    # look inside this line's own zone (halfway to the neighbours)
    c0, c1 = max(cy0, Z0), min(cy1, Z1)
    if c1 - c0 < 3:
        c0, c1 = Z0, Z1
    if cx1 - cx0 < 3 or Z1 - Z0 < 3:
        return None
    # background: thin ring 2..5 px around the OCR box
    R0, S0, R1, S1 = max(0, cx0 - 5), max(0, cy0 - 5), min(W, cx1 + 5), min(H, cy1 + 5)
    ring_mask = np.ones((S1 - S0, R1 - R0), bool)
    ring_mask[max(0, cy0 - 2 - S0):cy1 + 2 - S0, max(0, cx0 - 2 - R0):cx1 + 2 - R0] = False
    ring = flat[S0:S1, R0:R1][ring_mask].astype(np.int16)
    if len(ring) < 8:
        return None
    bg = np.median(ring, axis=0)
    uniform = float((np.abs(ring - bg).max(axis=1) <= 24).mean())
    roi = flat[Z0:Z1, cx0:cx1].astype(np.int16)
    dist = np.abs(roi - bg).max(axis=2)
    core = dist[c0 - Z0:c1 - Z0]
    if core.size == 0 or core.max() < 40:
        return None
    thr = max(40.0, 0.4 * float(core.max()))
    mask = dist > thr
    sat_all = roi.max(axis=2) - roi.min(axis=2)

    def band_of(m):
        rows = m.sum(axis=1)
        # main band: from the densest row inside the box, grow while rows are not blank
        # (a single blank row is bridged: accelerator-key underlines sit 1 px below the text)
        r = c0 - Z0 + int(np.argmax(rows[c0 - Z0:c1 - Z0]))
        t = r
        while t > 0 and (rows[t - 1] > 0 or (t > 1 and rows[t - 2] > 0 and t - 2 >= c0 - Z0 - 2)):
            t -= 1
        b = r
        while b < len(rows) - 1 and (rows[b + 1] > 0 or (b < len(rows) - 2 and rows[b + 2] > 0
                                                          and b + 2 <= c1 - Z0 + 2)):
            b += 1
        return rows, t, b

    rows, top, bot = band_of(mask)
    band = mask[top:bot + 1]
    cols = np.nonzero(band.sum(axis=0))[0]
    if len(cols) == 0 or band.sum() < 4:
        return None
    # drop a coloured icon that the OCR box swallowed at either end (app icons in title bars)
    groups = np.split(cols, np.nonzero(np.diff(cols) > 0.25 * (y1 - y0))[0] + 1)
    if len(groups) >= 2:
        def colourful(g):
            m = band[:, g[0]:g[-1] + 1]
            return float((sat_all[top:bot + 1, g[0]:g[-1] + 1][m] >= 60).mean()) if m.any() else 0.0
        sats = [colourful(g) for g in groups]
        keep = list(range(len(groups)))
        if sats[0] > 0.5 and max(sats[1:]) < 0.3:
            keep.remove(0)
        if len(keep) > 1 and sats[-1] > 0.5 and max(sats[i] for i in keep[:-1]) < 0.3:
            keep.remove(len(groups) - 1)
        if len(keep) < len(groups):
            lo, hi = groups[keep[0]][0], groups[keep[-1]][-1]
            mask[:, :lo] = False
            mask[:, hi + 1:] = False
            rows, top, bot = band_of(mask)
            band = mask[top:bot + 1]
            cols = np.nonzero(band.sum(axis=0))[0]
            if len(cols) == 0:
                return None
    ink = [cx0 + int(cols[0]), Z0 + top, cx0 + int(cols[-1]) + 1, Z0 + bot + 1]
    peak = rows[top:bot + 1].max()
    dense = np.nonzero(rows[top:bot + 1] >= 0.35 * peak)[0]
    dense_h = float(dense[-1] - dense[0] + 1)
    full_h = float(bot - top + 1)
    em = em_of(text, full_h, dense_h)
    # stroke width (distance transform) -> bold detection
    dt = cv2.distanceTransform(band.astype(np.uint8), cv2.DIST_L2, 3)
    stroke = float(2 * dt[band].mean())
    # text colour: prefer the neutral (grey/black/white) strokes over coloured icons in the box
    px = roi[top:bot + 1][band]
    strong = px[dist[top:bot + 1][band] >= np.percentile(dist[top:bot + 1][band], 50)]
    sat = strong.max(axis=1) - strong.min(axis=1)
    neutral = strong[sat < 40]
    fg = np.median(neutral if len(neutral) >= 0.15 * len(strong) else strong, axis=0)
    erase = [max(0, ink[0] - 2), max(Z0, ink[1] - 1), min(W, ink[2] + 2), min(Z1, ink[3] + 1)]
    a_ring = alpha[S0:S1, R0:R1][ring_mask]
    return {"bg": [int(v) for v in bg], "fg": [int(v) for v in fg], "uniform": round(uniform, 3),
            "ink": ink, "erase": erase, "zone": [Z0, Z1], "em": round(em, 2), "stroke": round(stroke, 2),
            "bg_alpha": int(np.median(a_ring)), "thr": round(thr, 1)}


def group_paragraphs(lines: list[dict]) -> list[list[dict]]:
    """Merge wrapped prose lines (same left edge, small gap, similar height) into paragraphs."""
    lines = sorted(lines, key=lambda l: (l["box"][1], l["box"][0]))
    paras: list[list[dict]] = []
    for B in lines:
        hb = B["box"][3] - B["box"][1]
        target = None
        if is_prose(B["text"]):
            for P in paras:
                L = P[-1]
                hl = L["box"][3] - L["box"][1]
                gap = B["box"][1] - L["box"][3]
                # (OCR box heights vary with ascenders/descenders, hence the loose ratio)
                # A wrapped sentence: the previous line is a long run of words (table columns
                # and lists are short), has no final punctuation, and the next line is not a
                # "Label: value" field.  Conservative on purpose: a sentence split in two
                # segments can still be translated part by part, a label glued into a
                # sentence cannot.
                long_prev = (L["box"][2] - L["box"][0]) >= 10 * hl or len(L["text"].split()) >= 4
                if (is_prose(L["text"]) and long_prev
                        and 0.65 < hb / hl < 1.55 and -0.25 * hl < gap < 0.7 * min(hl, hb)
                        and abs(B["box"][0] - P[0]["box"][0]) < 0.8 * hl
                        and L["box"][2] >= B["box"][2] - 2 * hl
                        and not re.search(r"[.:!?。),]\s*$", L["text"])
                        and not (re.match(r"\s*[A-Z]", B["text"]) and ":" in B["text"])):
                    target = P
                    break
        if target is not None:
            target.append(B)
        else:
            paras.append([B])
    return paras


# --------------------------------------------------------------------------- record
def record(pdf: str, work: Path, lang: str, engine: str = "auto", reuse_ocr: bool = False,
           carry_dirs: list[Path] | None = None, pages: str | None = None) -> dict:
    imgdir = work / "img"
    imgdir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf)
    imgs = content_images(doc, parse_pages(pages))
    paths, data = [], {}
    for xref, places in imgs.items():
        try:
            rgba = load_rgba(doc, xref)
        except Exception as e:  # noqa: BLE001 - exotic image encodings: leave that image alone
            print(f"images: skipped image {xref} ({e})")
            continue
        p = imgdir / f"x{xref}.png"
        save_png(p, flatten(rgba))
        paths.append(p)
        data[str(xref)] = {"places": places, "size": [rgba.shape[1], rgba.shape[0]], "paras": []}
    raw_p = imgdir / "ocr_raw.json"
    raw = load_json(raw_p, {}) if reuse_ocr else {}
    ocr: dict = {}
    if paths:
        if raw.get("engine") == "rapidocr" and raw.get("lang") == rec_lang(lang):
            ocr = raw["lines"]
        else:
            try:
                ocr = run_ocr(paths, lang)
                raw_p.write_text(json.dumps({"engine": "rapidocr", "lang": rec_lang(lang), "lines": ocr},
                                            ensure_ascii=False), "utf-8")
            except Exception as e:  # noqa: BLE001 - no model / no network: translate the body text only
                print(f"WARNING images: OCR failed ({type(e).__name__}: {e}); text inside images stays as is")
                ocr = {}

    segs, sid = [], 0
    for p in paths:
        xref = p.stem[1:]
        rgba = load_rgba(doc, int(xref))
        flat, alpha = flatten(rgba), rgba[:, :, 3]
        cand = [l for l in ocr.get(str(p), []) if keep_line(l, lang)]
        lines = []
        for l, z in zip(cand, line_zones(cand)):
            st = line_style(flat, alpha, l["box"], l["text"], z)
            if st:
                lines.append({**l, **st})
        paras = group_paragraphs(lines)
        if paras:  # reading order: rows (quantised by line height), then left to right
            q = 0.6 * float(np.median([l["box"][3] - l["box"][1] for l in lines]))
            paras.sort(key=lambda P: (round((P[0]["box"][1] + P[0]["box"][3]) / 2 / q), P[0]["box"][0]))
        for para in paras:
            sid += 1
            src = " ".join(l["text"] for l in para)
            data[xref]["paras"].append({"id": sid, "src": src, "lines": para})
            segs.append({"id": sid, "src": src, "img": int(xref), "page": data[xref]["places"][0]["page"] + 1})
    (imgdir / "ocr.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")

    carry: dict[str, str] = {}
    for d in [work, *(carry_dirs or [])]:  # earlier runs of this job, then other jobs
        old_segs = load_json(d / "img_segments.json", [])
        old_tr = load_json(d / "img_translations.json", {})
        for s in old_segs:
            if str(s["id"]) in old_tr:
                carry.setdefault(s["src"], old_tr[str(s["id"])])
    trans = {str(s["id"]): carry[s["src"]] for s in segs if s["src"] in carry}
    for s in segs:  # nothing to translate: numbers, versions, URLs / domains
        if str(s["id"]) not in trans and (not any(c.isalpha() for c in s["src"]) or URL_RE.match(s["src"])):
            trans[str(s["id"])] = s["src"]
    seen: set[str] = set()
    todo = []  # one entry per distinct source text; apply() shares translations by source text
    for s in segs:
        if str(s["id"]) not in trans and s["src"] not in seen:
            seen.add(s["src"])
            todo.append(s)
    (work / "img_segments.json").write_text(json.dumps(segs, ensure_ascii=False, indent=1), "utf-8")
    (work / "img_translations.json").write_text(json.dumps(trans, ensure_ascii=False, indent=1), "utf-8")
    (work / "img_todo.json").write_text(json.dumps(todo, ensure_ascii=False, indent=1), "utf-8")
    print(f"images: {len(paths)} OCRed, {len(segs)} text segments; carried over {len(trans)}; todo {len(todo)}")
    return {"images": len(paths), "segments": len(segs), "todo": len(todo)}


def load_json(p: Path, default):
    return json.loads(p.read_text("utf-8")) if p.exists() else default


# --------------------------------------------------------------------------- render
TOKEN = re.compile(r"[A-Za-z0-9_.,:;'\"()\[\]{}<>/\\@#%&*+=!?$-]+|\s+|.", re.S)
NO_LINE_START = set("，。、；：！？）》」』】,.;:!?)]}%")


def wrap(text: str, font: ImageFont.FreeTypeFont, width: float) -> list[str]:
    if "\n" in text:  # explicit line breaks from the translator (e.g. title / subtitle)
        return [l for part in text.split("\n") for l in wrap(part, font, width)]
    lines, cur = [], ""
    for t in TOKEN.findall(text):
        if t.isspace():
            if cur:
                cur += " "
            continue
        trial = cur + t
        if not cur.strip() or font.getlength(trial) <= width or t[0] in NO_LINE_START:
            cur = trial
        else:
            lines.append(cur.rstrip())
            cur = t
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or [""]


def _font(bold: bool, size: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(F.cjk(bold), max(6, int(round(size))))


def extend_erase(flat: np.ndarray, l: dict, kept: list[dict]) -> dict:
    """Grow the erase rect vertically over glyph parts the record-time zone cut off (descenders
    next to another line), now that we know which neighbours stay: only kept lines block."""
    x0, y0, x1, y1 = l["erase"]
    H = flat.shape[0]
    em = l["em"]
    top_lim, bot_lim = max(0, int(y0 - 0.5 * em)), min(H, int(math.ceil(y1 + 0.5 * em)))
    cy = (y0 + y1) / 2
    for k in kept:
        kx0, ky0, kx1, ky1 = k["ink"]
        if min(x1, kx1) - max(x0, kx0) <= 0:
            continue
        # a kept neighbour stops the growth even when its (padded) box overlaps ours
        if (ky0 + ky1) / 2 < cy:
            top_lim = max(top_lim, min(ky1, y0))
        else:
            bot_lim = min(bot_lim, max(ky0, y1))
    bg = np.array(l["bg"], np.int16)
    thr = 0.6 * l["thr"]

    def has_ink(y):
        return bool((np.abs(flat[y, x0:x1].astype(np.int16) - bg).max(axis=1) > thr).any())

    while y0 - 1 >= top_lim and has_ink(y0 - 1):
        y0 -= 1
    while y1 < bot_lim and has_ink(y1):
        y1 += 1
    z0, z1 = l.get("zone", [y0, y1])
    return dict(l, erase=[x0, y0, x1, y1], zone=[min(z0, y0), max(z1, y1)])


def erase_line(img: np.ndarray, flat: np.ndarray, l: dict, s: int):
    """img: upscaled RGBA (modified in place); flat: original-size flattened RGB."""
    X0, Y0, X1, Y1 = (v * s for v in l["erase"])
    # "uniform" = share of ring pixels equal to the background; neighbouring lines and frame
    # borders take up to ~40% of the ring, photos / gradients score far lower
    if l["uniform"] >= 0.6:
        if l["bg_alpha"] < 128:
            img[Y0:Y1, X0:X1] = 0                     # transparent background
            return
        # JPEG ringing around the old glyphs spreads over the surrounding 8x8 blocks and shows
        # up as grey dust on the flat background: snap near-background pixels there to bg
        x0, y0, x1, y1 = l["erase"]
        Hh, Ww = flat.shape[:2]
        # (vertically only the blocks the glyphs touch: one more block may be another panel,
        # e.g. a white header right under a grey title bar, which is "near" the background too)
        gx0, gx1 = max(0, (x0 // 8) * 8 - 8), min(Ww, ((x1 + 7) // 8) * 8 + 8)
        z0, z1 = l.get("zone", [y0, y1])
        gy0, gy1 = max(z0, (y0 // 8) * 8, 0), min(z1, ((y1 + 7) // 8) * 8, Hh)
        near = np.abs(flat[gy0:gy1, gx0:gx1].astype(np.int16) - np.array(l["bg"])).max(axis=2) <= 30
        if s > 1:
            near = cv2.resize(near.astype(np.uint8), None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST) > 0
        sub = img[gy0 * s:gy1 * s, gx0 * s:gx1 * s]
        sub[near, :3] = l["bg"]
        img[Y0:Y1, X0:X1, :3] = l["bg"]
        img[Y0:Y1, X0:X1, 3] = l["bg_alpha"]
        return
    # textured background: inpaint just the text strokes
    x0, y0, x1, y1 = l["erase"]
    roi = flat[y0:y1, x0:x1].astype(np.int16)
    mask = (np.abs(roi - np.array(l["bg"])).max(axis=2) > 0.6 * l["thr"]).astype(np.uint8) * 255
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
    if s > 1:
        mask = cv2.resize(mask, None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST)
    rgb = np.ascontiguousarray(img[Y0:Y1, X0:X1, :3])
    img[Y0:Y1, X0:X1, :3] = cv2.inpaint(rgb, mask, 3, cv2.INPAINT_TELEA)


# Stroke width / em above this = bold face.  At screenshot sizes (11-15 px) antialiasing makes
# regular and bold strokes nearly equal (0.14-0.18 measured), so bold is effectively disabled.
BOLD_STROKE = 0.30


def layout_para(para: dict, text: str, s: int, left_edges: list[float], em: float, img_w: int) -> list[dict]:
    """Fit the translation into the paragraph area -> [{text, x, y(center), font, fg}]."""
    lines = para["lines"]
    inks = [l["ink"] for l in lines]
    X0 = min(b[0] for b in inks)
    X1 = max(b[2] for b in inks)
    cys = [(b[1] + b[3]) / 2 * s for b in inks]
    n = len(lines)
    pitch = (cys[-1] - cys[0]) / (n - 1) if n > 1 else em * 1.35 * s
    bold = float(np.median([l["stroke"] / max(l["em"], 1) for l in lines])) > BOLD_STROKE
    fg = tuple(int(v) for v in np.median([l["fg"] for l in lines], axis=0))
    hb = lines[0]["box"][3] - lines[0]["box"][1]
    aligned = sum(abs(e - lines[0]["box"][0]) < 0.6 * hb for e in left_edges) > 1
    center = n == 1 and not aligned
    size0 = em * 0.92 * s
    if n > 1:
        size0 = min(size0, pitch / 1.15)
    width = (X1 - X0) * s
    if n == 1:  # a single line may grow a little into the margin rather than shrink
        width = min(width * (1.25 if center else 1.12), (img_w - X0) * s - 2)

    choice = None
    for f in np.arange(1.0, 0.59, -0.05):
        font = _font(bold, size0 * f)
        wl = wrap(text, font, width)
        if len(wl) <= n:
            choice = (font, wl, pitch)
            break
    if choice is None:  # still too long at 60%: wrap into extra lines, squeezing the pitch
        font = _font(bold, size0 * 0.75)
        wl = wrap(text, font, width)
        lp = max(font.size * 1.1, min(pitch, pitch * n / len(wl)))
        choice = (font, wl, lp)
    font, wl, lp = choice
    out = []
    y_first = cys[0] if len(wl) <= n else cys[0] - (len(wl) - n) * lp / 2 * (n == 1)
    for i, t in enumerate(wl):
        w = font.getlength(t)
        x = (X0 + X1) / 2 * s - w / 2 if center else X0 * s
        out.append({"text": t, "x": x, "y": y_first + i * lp, "font": font, "fg": fg})
    return out


def render_image(rgba: np.ndarray, paras: list[dict], trans: dict[str, str]) -> np.ndarray | None:
    """Return the edited RGBA image (possibly upscaled), or None if nothing to change."""
    todo = [(p, trans[str(p["id"])]) for p in paras
            if str(p["id"]) in trans and trans[str(p["id"])].strip() != p["src"].strip()]
    if not todo:
        return None
    flat = flatten(rgba)
    all_lines = [l for p in paras for l in p["lines"]]
    em_all = [l["em"] for l in all_lines]
    em_med = float(np.median([l["em"] for p, _ in todo for l in p["lines"]]))
    s = int(min(2, max(1, math.ceil(20 / max(em_med, 1)))))  # small screenshot text: draw at 2x
    img = cv2.resize(rgba, None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST) if s > 1 else rgba.copy()
    todo_ids = {p["id"] for p, _ in todo}
    kept = [l for p in paras if p["id"] not in todo_ids for l in p["lines"]]
    for p, _ in todo:
        for l in p["lines"]:
            erase_line(img, flat, extend_erase(flat, l, kept), s)
    left_edges = [l["box"][0] for l in all_lines]
    pil = Image.fromarray(img, "RGBA")
    layer = Image.new("RGBA", pil.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for p, t in todo:
        if not t.strip():
            continue
        em = float(np.median([l["em"] for l in p["lines"]]))
        # snap to the image's common sizes: buttons / labels of one dialog get one size
        em = float(np.median([e for e in em_all if abs(e - em) <= 0.25 * em]))
        for item in layout_para(p, t.strip(), s, left_edges, em, rgba.shape[1]):
            draw.text((item["x"], item["y"]), item["text"], font=item["font"], fill=item["fg"] + (255,),
                      anchor="lm")
    return np.array(Image.alpha_composite(pil, layer))


def edited_images(pdf: str, work: Path) -> dict[int, np.ndarray]:
    data = load_json(work / "img" / "ocr.json", {})
    trans = dict(load_json(work / "img_translations.json", {}))
    by_src = {}
    for d in data.values():
        for p in d["paras"]:
            if str(p["id"]) in trans:
                by_src.setdefault(p["src"], trans[str(p["id"])])
    for d in data.values():  # repeated texts ("Next >", "Cancel") are translated once
        for p in d["paras"]:
            if str(p["id"]) not in trans and p["src"] in by_src:
                trans[str(p["id"])] = by_src[p["src"]]
    doc = fitz.open(pdf)
    out = {}
    for xref, d in data.items():
        if not d["paras"]:
            continue
        new = render_image(load_rgba(doc, int(xref)), d["paras"], trans)
        if new is not None:
            out[int(xref)] = new
    return out


def preview(pdf: str, work: Path):
    outdir = work / "img" / "preview"
    outdir.mkdir(parents=True, exist_ok=True)
    for xref, img in edited_images(pdf, work).items():
        save_png(outdir / f"x{xref}.png", flatten(img))
        print(outdir / f"x{xref}.png")


# --------------------------------------------------------------------------- apply to PDFs
def _pixmap(img: np.ndarray) -> fitz.Pixmap:
    if (img[:, :, 3] == 255).all():
        rgb = np.ascontiguousarray(img[:, :, :3])
        return fitz.Pixmap(fitz.csRGB, rgb.shape[1], rgb.shape[0], rgb.tobytes(), False)
    return fitz.Pixmap(fitz.csRGB, img.shape[1], img.shape[0], np.ascontiguousarray(img).tobytes(), True)


def apply(pdf: str, mono: str, work: Path, dual: str | None = None, dual_left: str | None = None) -> int:
    edits = edited_images(pdf, work)
    if not edits:
        print("images translated: 0")
        return 0
    data = load_json(work / "img" / "ocr.json", {})
    src = fitz.open(pdf)
    out = fitz.open(mono)
    done: set[int] = set()
    for xref, img in edits.items():
        # smallest of PNG / JPEG (JPEG only for opaque JPEG sources): flat screenshots compress
        # far better as PNG, photo-like ones as JPEG
        kw = {"pixmap": _pixmap(img)}
        if (img[:, :, 3] == 255).all():
            bgr = cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2BGR)
            cands = [cv2.imencode(".png", bgr, [cv2.IMWRITE_PNG_COMPRESSION, 9])[1].tobytes()]
            if "DCTDecode" in src.xref_get_key(xref, "Filter")[1]:
                cands.append(cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes())
            kw = {"stream": min(cands, key=len)}
        for place in data[str(xref)]["places"]:
            page = out[place["page"]]
            target = fitz.Rect(place["bbox"])
            for info in page.get_image_info(xrefs=True):
                ox = info.get("xref", 0)
                b = fitz.Rect(info["bbox"]) & page.rect
                if ox and ox not in done and abs(b.x0 - target.x0) < 1.5 and abs(b.y0 - target.y0) < 1.5 \
                        and abs(b.x1 - target.x1) < 1.5 and abs(b.y1 - target.y1) < 1.5:
                    page.replace_image(ox, **kw)
                    done.add(ox)
    tmp = mono + ".tmp.pdf"
    out.save(tmp, garbage=3, deflate=True)
    out.close()
    os.replace(tmp, mono)
    if dual:
        build_dual(dual_left or pdf, mono, dual)
    print(f"images translated: {len(edits)} (replaced {len(done)} in {Path(mono).name})")
    return len(edits)


def build_dual(orig: str, mono: str, dual: str):
    """Side-by-side pages: original on the left, translation on the right (as BabelDOC does)."""
    a, b = fitz.open(orig), fitz.open(mono)
    d = fitz.open()
    for i in range(min(a.page_count, b.page_count)):
        r = a[i].rect
        page = d.new_page(width=r.width * 2, height=r.height)
        page.show_pdf_page(fitz.Rect(0, 0, r.width, r.height), a, i)
        page.show_pdf_page(fitz.Rect(r.width, 0, r.width * 2, r.height), b, i)
    tmp = dual + ".tmp.pdf"
    d.save(tmp, garbage=3, deflate=True)
    d.close()
    os.replace(tmp, dual)


# --------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("pdf")
    r.add_argument("work")
    r.add_argument("--lang", default="en")
    r.add_argument("--engine", default="auto", choices=["auto", "rapidocr"])
    r.add_argument("--reuse-ocr", action="store_true", help="reuse <work>/img/ocr_raw.json")
    v = sub.add_parser("preview")
    v.add_argument("pdf")
    v.add_argument("work")
    a = sub.add_parser("apply")
    a.add_argument("pdf")
    a.add_argument("mono")
    a.add_argument("work")
    a.add_argument("--dual", default=None)
    w = sub.add_parser("warmup")
    w.add_argument("--lang", default="en,ko")
    args = ap.parse_args()
    if args.cmd == "record":
        record(args.pdf, Path(args.work), args.lang, args.engine, args.reuse_ocr)
    elif args.cmd == "warmup":
        warmup([x for x in args.lang.split(",") if x])
    elif args.cmd == "preview":
        preview(args.pdf, Path(args.work))
    else:
        apply(args.pdf, args.mono, Path(args.work), args.dual)


if __name__ == "__main__":
    main()
