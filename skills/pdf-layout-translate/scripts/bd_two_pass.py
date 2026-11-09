"""BabelDOC two-pass wrapper: no translation API needed.

Pass 1 (record): run BabelDOC with a translator that returns the source text and
records every paragraph BabelDOC wants translated -> <work>/segments.json.
Existing translations (same source text) are carried over into translations.json;
the ids still needing a translation go to <work>/todo.json.
Then the agent (or a person) fills <work>/translations.json  {"<id>": "<译文>"}.
Pass 2 (replay): run BabelDOC again; the translator looks translations up by source
text, so BabelDOC does all layout work (formula protection, re-typesetting, fonts).

usage:
  python bd_two_pass.py record <pdf> <work> [--lang-in en] [--preprocess] [--layout slide]
                                            [--carry-from <other work dir>]
  python bd_two_pass.py replay <pdf> <work> [same options as record]
add --ocr-images to both passes to also translate text inside screenshots / figures
(image_translate.py: local OCR, translations in <work>/img_translations.json).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import babeldoc.format.pdf.high_level as high_level
from babeldoc.docvision.doclayout import DocLayoutModel
from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
from babeldoc.translator.translator import BaseTranslator, set_translate_rate_limiter

sys.path.insert(0, str(Path(__file__).parent))
import image_translate  # noqa: E402
import slide_tools  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
# "watermark output mode is deprecated" on every run: noise for our use
logging.getLogger("babeldoc.format.pdf.translation_config").setLevel(logging.ERROR)

# CJK fonts draw these bullets full-width; swap for the small equivalents.
BULLET_FIX = {"○": "◦", "●": "•", "■": "▪", "□": "▫"}
BULLET_RE = re.compile(r"^(\s*)([○●■□])")
# Placeholders that only hide plain punctuation/digits (e.g. the '_' in emp_super) are
# written back as literal text: BabelDOC mis-positions such tiny "formulas" vertically.
PLAIN_PH = re.compile(r"^[\[\]_0-9.,:;()'\"/%#&*+=<>! \-]+$")
# BabelDOC typesets into the width of the ORIGINAL text, so every extra space costs
# font size.  Drop spaces between CJK and Latin/digits (common mainland style, like WPS).
CJK = r"　-〿一-鿿＀-￯"
SP_AFTER_CJK = re.compile(rf"(?<=[{CJK}])[ \t]+(?=[0-9A-Za-z({{\[<'\"‘“])")
SP_BEFORE_CJK = re.compile(rf"(?<=[0-9A-Za-z)}}\]>'\"’”%])[ \t]+(?=[{CJK}])")
LEAD_MARKER = re.compile(r"^\s*(?:[●○■□◆◇❖•◦▪▫‣➢➤✓]|(?:[a-zA-Z]|\d{1,2})[.)])\s*")


def compact(text: str) -> str:
    return SP_BEFORE_CJK.sub("", SP_AFTER_CJK.sub("", text))


def placeholder_map(work: Path, pages: str | None = None) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """From BabelDOC's debug tracking file: source text -> {placeholder: original chars}, and
    source text -> page number (tracking pages are the translated pages, in order)."""
    out: dict[str, dict[str, str]] = {}
    page_of: dict[str, int] = {}
    sel = sorted(image_translate.parse_pages(pages)) if pages else None
    for f in (work / "tmp").glob("*/translate_tracking.json"):
        data = json.loads(f.read_text("utf-8"))
        for i, page in enumerate(data.get("page", [])):
            pno = (sel[i] if sel and i < len(sel) else i) + 1
            for p in page.get("paragraph", []):
                page_of.setdefault(p["input"], pno)
                phs = {x["placeholder"]: x.get("formula_chars", "") for x in p.get("placeholders", [])
                       if x.get("type") == "formula"}
                if phs:
                    out[p["input"]] = phs
    return out, page_of


def load_json(p: Path, default):
    return json.loads(p.read_text("utf-8")) if p.exists() else default


def known_translations(*dirs: Path) -> dict[str, str]:
    table: dict[str, str] = {}
    for d in dirs:
        if not d:
            continue
        segs = load_json(d / "segments.json", [])
        trans = load_json(d / "translations.json", {})
        for s in segs:
            t = trans.get(str(s["id"]))
            if t is not None:
                table.setdefault(s["src"], t)
                # list markers are now split into their own boxes: also offer the
                # marker-less pair so old translations still match
                if LEAD_MARKER.match(s["src"]) and LEAD_MARKER.match(t):
                    table.setdefault(LEAD_MARKER.sub("", s["src"], count=1), LEAD_MARKER.sub("", t, count=1))
                # bullets that BabelDOC had turned into a leading {v1} placeholder
                if s["src"].startswith("{v1}") and t.startswith("{v1}"):
                    rest_s, rest_t = s["src"][4:], t[4:]
                    renum = lambda x: re.sub(r"\{v(\d+)\}", lambda m: "{v%d}" % (int(m.group(1)) - 1), x)
                    table.setdefault(renum(rest_s), renum(rest_t))
    return table


def renum(text: str, delta: int) -> str:
    return re.sub(r"\{v(\d+)\}", lambda m: "{v%d}" % (int(m.group(1)) + delta), text)


def carry_lookup(src: str, carry: dict[str, str]) -> str | None:
    """Exact match, else re-attach a leading bullet placeholder / list marker to a
    translation that was made for the marker-less text."""
    if src in carry:
        return carry[src]
    if src.startswith("{v1}"):
        t = carry.get(renum(src[4:], -1))
        if t is not None:
            return "{v1}" + renum(t, 1)
    m = LEAD_MARKER.match(src)
    if m:
        t = carry.get(src[m.end():])
        if t is not None:
            return m.group(0).strip() + " " + t
    return None


class ReplayTranslator(BaseTranslator):
    name = "replay"

    def __init__(self, lang_in, lang_out, mode: str, work: Path, keep_spaces: bool = False):
        super().__init__(lang_in, lang_out, ignore_cache=True)
        self.keep_spaces = keep_spaces
        self.model = "replay"
        self.mode = mode
        self.work = work
        self.lock = threading.Lock()
        self.order: list[str] = []
        self.seen: set[str] = set()
        self.missing: set[str] = set()
        self.table = known_translations(work) if mode == "replay" else {}
        self.ph = {s["src"]: s.get("ph", {}) for s in load_json(work / "segments.json", [])} if mode == "replay" else {}

    # Same placeholder style as BabelDOC's OpenAITranslator.
    def get_formular_placeholder(self, placeholder_id):
        return "{v" + str(placeholder_id) + "}", f"{{\\s*v\\s*{placeholder_id}\\s*}}"

    def get_rich_text_left_placeholder(self, placeholder_id):
        return (
            f"<style id='{placeholder_id}'>",
            f"<\\s*style\\s*id\\s*=\\s*'\\s*{placeholder_id}\\s*'\\s*>",
        )

    def get_rich_text_right_placeholder(self, placeholder_id):
        return "</style>", r"<\s*\/\s*style\s*>"

    def do_llm_translate(self, text, rate_limit_params=None):
        # Force BabelDOC onto the plain per-paragraph translate() path.
        raise NotImplementedError

    def do_translate(self, text, rate_limit_params=None):
        with self.lock:
            if text not in self.seen:
                self.seen.add(text)
                self.order.append(text)
            if self.mode == "record":
                return text
            if text in self.table:
                if self.table[text] == text:  # "keep as is" (code, names...): same as untranslated
                    return text
                out = BULLET_RE.sub(lambda m: m.group(1) + BULLET_FIX[m.group(2)], self.table[text])
                for ph, chars in self.ph.get(text, {}).items():
                    if chars and PLAIN_PH.match(chars):
                        out = out.replace(ph, chars)
                return out if self.keep_spaces else compact(out)
            self.missing.add(text)
            return text

    def dump(self, carry: dict[str, str], pages: str | None = None):
        if self.mode == "record":
            phmap, page_of = placeholder_map(self.work, pages)
            segs = []
            for i, s in enumerate(self.order):
                seg = {"id": i + 1, "src": s}
                if s in page_of:
                    seg["page"] = page_of[s]
                if phmap.get(s):
                    seg["ph"] = phmap[s]
                segs.append(seg)
            trans = {}
            for s in segs:
                t = carry_lookup(s["src"], carry)
                if t is None and not any(c.isalpha() for c in re.sub(r"\{v\d+\}|</?style[^>]*>", "", s["src"])):
                    t = s["src"]  # numbers / placeholders only: nothing to translate
                if t is not None:
                    trans[str(s["id"])] = t
            todo = [s for s in segs if str(s["id"]) not in trans]
            (self.work / "segments.json").write_text(json.dumps(segs, ensure_ascii=False, indent=1), "utf-8")
            (self.work / "translations.json").write_text(json.dumps(trans, ensure_ascii=False, indent=1), "utf-8")
            (self.work / "todo.json").write_text(json.dumps(todo, ensure_ascii=False, indent=1), "utf-8")
            print(f"recorded {len(segs)} segments; carried over {len(trans)}; todo {len(todo)}")
        else:
            (self.work / "missing.json").write_text(
                json.dumps(sorted(self.missing), ensure_ascii=False, indent=1), "utf-8")
            print(f"replayed {len(self.order) - len(self.missing)}/{len(self.order)} unique; "
                  f"left as source {len(self.missing)} -> missing.json")


def has_text(pdf: str, pages: str | None = None) -> bool:
    """Any visible text on the selected pages?  (BabelDOC refuses PDFs without paragraphs.)"""
    import pymupdf

    sel = image_translate.parse_pages(pages)
    doc = pymupdf.open(pdf)
    return any(p.get_text().strip() for p in doc if sel is None or p.number in sel)


async def _no_events():
    return
    yield  # noqa: unreachable - makes this an (empty) async generator


async def run(args):
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    out = work / ("record_out" if args.mode == "record" else "out")
    out.mkdir(exist_ok=True)

    # Input clean-ups for BabelDOC only (nothing visible changes; outputs keep the original as
    # the left side of the dual PDF).  record regenerates them, replay reuses the same files.
    src_pdf = args.pdf

    def stage(name: str, fn, report):
        nonlocal src_pdf
        dst = work / name
        if args.mode == "record" or not dst.exists():
            print(report(fn(src_pdf, str(dst))))
        src_pdf = str(dst)

    if args.preprocess:
        stage("pre.pdf", slide_tools.preprocess, lambda rep: (
            f"preprocess: merged {sum(p['inline_math'] for p in rep['pages'])} inline-math spans, "
            f"removed {sum(p['hidden_words'] for p in rep['pages'])} hidden words"))
    if getattr(args, "normalize_gstate", False):
        stage("pre_gs.pdf", slide_tools.normalize_text_state,
              lambda k: f"faux-bold stroke state removed from {k} text runs")
    vis = work / "pre_vis.pdf"
    if (args.mode == "replay" and vis.exists()) or (args.mode == "record" and slide_tools.count_invisible_text(src_pdf)):
        stage("pre_vis.pdf", slide_tools.drop_invisible_text,
              lambda k: f"invisible text (OCR layer of scans) removed: {k} runs")
    elif args.mode == "record":
        vis.unlink(missing_ok=True)

    carry = {}
    if args.mode == "record":
        extra = [Path(p) for p in args.carry_from.split(",")] if args.carry_from else []
        carry = known_translations(work, *extra)
        shutil.rmtree(work / "tmp", ignore_errors=True)  # stale tracking files would mislead placeholder_map

    set_translate_rate_limiter(1000)
    tr = ReplayTranslator(args.lang_in, args.lang_out, args.mode, work, keep_spaces=args.keep_spaces)
    layout = DocLayoutModel.load_onnx()
    if args.layout == "slide":
        layout = slide_tools.make_slide_layout(layout)
    if not args.no_refine:
        layout = slide_tools.make_refined_layout(layout, getattr(args, "slide_breaks", False))
    cfg = TranslationConfig(
        translator=tr,
        input_file=src_pdf,
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        doc_layout_model=layout,
        output_dir=str(out),
        working_dir=str(work / "tmp"),
        qps=8,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        auto_extract_glossary=False,
        save_auto_extracted_glossary=False,
        deterministic_output=True,
        no_dual=args.mode == "record",
        use_rich_pbar=False,
        pages=args.pages,
        primary_font_family=args.font_family,
        debug=args.debug or args.mode == "record",  # record needs translate_tracking.json
        min_text_length=1,
        # scanned pages simply yield no text here; their pictures go through the image path
        skip_scanned_detection=True,
        # bullets inside a paragraph become formula placeholders -> original glyph kept
        formular_char_pattern=r"^[●○■□◆◇❖◦▪▫‣➢➤✓]$",
    )
    result = None
    no_text = not has_text(src_pdf, args.pages)
    async for ev in (_no_events() if no_text else high_level.async_translate(cfg)):
        if ev["type"] == "error":
            if "no paragraphs" in str(ev.get("error", "")).lower():
                no_text = True  # a scan / image-only PDF: only the image path has work to do
                break
            raise SystemExit(f"BabelDOC error: {ev['error']}")
        if ev["type"] == "finish":
            result = ev["translate_result"]
            break
    tr.dump(carry, args.pages)
    if args.mode == "record" and not args.debug:
        shutil.rmtree(work / "tmp", ignore_errors=True)  # debug files are large; placeholders already extracted
    stem = Path(args.pdf).stem
    if no_text:
        print("no text layer found (scanned / image-only PDF): only the text inside images is translated")
        if args.mode == "replay":
            mono = out / f"{stem}.no_watermark.zh.mono.pdf"
            shutil.copy2(args.pdf, mono)
            result = SimpleNamespace(mono_pdf_path=mono, dual_pdf_path=out / f"{stem}.no_watermark.zh.dual.pdf")
    print("result:", result)
    if args.ocr_images:
        # text inside screenshots / figures (BabelDOC leaves raster images untouched)
        if args.mode == "record":
            extra = [Path(p) for p in args.carry_from.split(",")] if args.carry_from else []
            image_translate.record(args.pdf, work, args.lang_in, args.ocr_engine, args.reuse_ocr,
                                   carry_dirs=extra, pages=args.pages)
        elif result is not None and (work / "img_segments.json").exists():
            mono = getattr(result, "mono_pdf_path", None)
            dual = getattr(result, "dual_pdf_path", None)
            if mono:
                image_translate.apply(args.pdf, str(mono), work, str(dual) if dual else None,
                                      dual_left=getattr(args, "dual_left", None))
    if args.mode == "replay" and result is not None:
        dual = getattr(result, "dual_pdf_path", None)
        if dual and not Path(dual).exists():  # no-text PDFs: BabelDOC wrote none
            image_translate.build_dual(getattr(args, "dual_left", None) or args.pdf, str(result.mono_pdf_path),
                                       str(dual))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["record", "replay"])
    ap.add_argument("pdf")
    ap.add_argument("work")
    ap.add_argument("--lang-in", default="en")
    ap.add_argument("--lang-out", default="zh")
    ap.add_argument("--pages", default=None)
    ap.add_argument("--font-family", default=None, choices=["serif", "sans-serif", "script"])
    ap.add_argument("--preprocess", action="store_true", help="merge Keynote-style inline math")
    ap.add_argument("--layout", default="yolo", choices=["yolo", "slide"])
    ap.add_argument("--carry-from", default=None)
    ap.add_argument("--no-refine", action="store_true", help="skip list/marker/cell box refinement")
    ap.add_argument("--keep-spaces", action="store_true", help="keep spaces between CJK and Latin")
    ap.add_argument("--slide-breaks", action="store_true",
                    help="slides: a row ending a sentence / 'Label:' ends the paragraph")
    ap.add_argument("--normalize-gstate", action="store_true",
                    help="Google Slides: drop faux-bold stroke state so text colour survives")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--ocr-images", action="store_true",
                    help="also translate text inside images (img_segments.json / img_translations.json)")
    ap.add_argument("--ocr-engine", default="auto", choices=["auto", "rapidocr"])
    ap.add_argument("--reuse-ocr", action="store_true", help="reuse <work>/img/ocr_raw.json on record")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
