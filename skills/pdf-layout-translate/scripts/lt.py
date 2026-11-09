"""pdf-layout-translate: layout-preserving PDF -> Simplified Chinese.

Run with any Python 3.9+; the first run installs a private runtime (about 1 GB, see
runtime_setup.py) and every command then re-runs itself inside it.

  lt.py setup [--force]          install / repair the runtime (done automatically when missing)
  lt.py check                    what is installed, where
  lt.py prepare <pdf> [--job DIR] [--lang-in auto|ko|en|ja|...] [--profile auto|keynote|slides|latex|generic]
                      [--pages 1-5,8] [--no-images] [--carry-from JOB[,JOB...]]
  lt.py todo <job> [--kind all|text|img] [--limit 60] [--offset 0] [--out todo.jsonl]
  lt.py merge <job> <translations.json | ->  keys "12" = text segment 12, "i7" = image segment 7
  lt.py build <job> [--out DIR] [--compare all|none|1-3,8]
  lt.py status <job>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lt_paths as P  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_PROSE = {"preprocess": False, "layout": "yolo", "font_family": None, "slide_breaks": False,
          "normalize_gstate": False}
PROFILES = {
    # Keynote: inline math drawn at the end of the stream + clipped hidden text -> preprocess;
    # its text boxes are reliable, the paper-trained YOLO model cuts big slide lines in two
    "keynote": {**_PROSE, "preprocess": True, "layout": "slide", "font_family": "sans-serif",
                "slide_breaks": True},
    # Google Slides / PowerPoint exports: YOLO boxes + list / table-cell refinement; lines that end
    # a sentence end the paragraph; Google's faux bold would otherwise turn coloured titles black
    "slides": {**_PROSE, "font_family": "sans-serif", "slide_breaks": True, "normalize_gstate": True},
    "latex": dict(_PROSE),
    "generic": dict(_PROSE),
}
PH_RE = re.compile(r"\{v\d+\}|<style id='\d+'>|</style>")
TEX_RE = re.compile(r"\b(?:pdf|lua|xe)?tex\b|latex|dvipdf|miktex|tex ?live", re.I)


def in_runtime() -> bool:
    try:
        return Path(sys.prefix).resolve() == P.VENV.resolve()
    except OSError:
        return False


def load_json(p: Path, default):
    return json.loads(p.read_text("utf-8")) if p.exists() else default


def save_json(p: Path, data, ascii_only: bool = False):
    # ascii_only for files holding user paths (undecodable file names survive as \\udcxx escapes)
    p.write_text(json.dumps(data, ensure_ascii=ascii_only, indent=1), "utf-8")


def need_job(path: str) -> Path:
    job = Path(path)
    if not (job / "job.json").exists():
        sys.exit(f"not a job folder (no job.json): {job}  - run `lt.py prepare <pdf>` first")
    return job


class JobLock:
    """prepare / build of one job must not run twice at once (an agent may retry a timed-out call)."""

    def __init__(self, job: Path):
        self.path = job / ".lock"

    def __enter__(self):
        import time

        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            if age > 3 * 3600 or (age > 2 and P.lock_owner_gone(self.path)):
                self.path.unlink(missing_ok=True)  # left by a killed run
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            sys.exit(f"this job is busy (another prepare/build is running): {self.path}\n"
                     "wait for it to finish; delete the .lock file only if nothing is running")
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def safe_copy(src: Path, dst: Path) -> Path:
    """Copy; if the target is open in a PDF viewer (Windows locks it), write next to it instead."""
    try:
        shutil.copy2(src, dst)
        return dst
    except PermissionError:
        for i in range(2, 100):
            alt = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
            try:
                shutil.copy2(src, alt)
                print(f"WARNING: {dst.name} is open in another program; saved as {alt.name}")
                return alt
            except PermissionError:
                continue
        raise


# --------------------------------------------------------------------------- detection
def detect_profile(doc) -> str:
    m = doc.metadata or {}
    c, p = (m.get("creator") or "").lower(), (m.get("producer") or "").lower()
    if "keynote" in c:
        return "keynote"
    if c.startswith("google") or "powerpoint" in c or "powerpoint" in p or "impress" in c:
        return "slides"
    # whole-word TeX engines only: macOS producers say "Quartz PDFContext"
    if TEX_RE.search(c) or TEX_RE.search(p):
        return "latex"
    r = doc[0].rect
    return "slides" if r.width > r.height * 1.2 else "generic"


def detect_lang(doc) -> str | None:
    text = "".join(doc[i].get_text() for i in range(min(10, doc.page_count)))
    n = {k: len(re.findall(rx, text)) for k, rx in {
        "ko": r"[가-힣]", "ja": r"[぀-ヿ]", "zh": r"[一-鿿]",
        "ru": r"[Ѐ-ӿ]", "en": r"[A-Za-z]"}.items()}
    total = sum(n.values())
    if total < 50:
        return None  # scanned / image-only: the caller must say which language
    if n["ko"] > 0.15 * total:
        return "ko"
    if n["ja"] > 0.05 * total:
        return "ja"
    if n["zh"] > 0.5 * total:
        return "zh"
    if n["ru"] > 0.3 * total:
        return "ru"
    return "en"  # Latin script: BabelDOC and the OCR "latin"/"ch" models cope with other Latin languages


# --------------------------------------------------------------------------- BabelDOC passes
def run_pass(job: Path, cfg: dict, mode: str, pdf: Path):
    import asyncio

    import bd_two_pass  # imported late: BabelDOC reads the user profile dir at import time

    o = {**_PROSE, **cfg["options"]}
    ns = argparse.Namespace(
        slide_breaks=o["slide_breaks"], normalize_gstate=o["normalize_gstate"],
        mode=mode, pdf=str(pdf), work=str(job), lang_in=cfg["lang_in"], lang_out="zh",
        pages=cfg.get("pages"), font_family=o["font_family"], preprocess=o["preprocess"],
        layout=o["layout"], carry_from=",".join(cfg.get("carry_from") or []) or None,
        no_refine=False, keep_spaces=False, debug=False, ocr_images=cfg["images"],
        ocr_engine="auto", reuse_ocr=False, dual_left=str(pdf))
    return asyncio.run(bd_two_pass.run(ns))


def default_job(pdf: Path) -> Path:
    stem = re.sub(r"[^\w.\-]+", "_", pdf.stem.encode("utf-8", "replace").decode("utf-8")).strip("._") or "doc"
    h = hashlib.sha1(os.fsencode(str(pdf.resolve())).lower()).hexdigest()[:8]
    return P.JOBS / f"{stem[:60]}-{h}"


# --------------------------------------------------------------------------- pending items
def pending(job: Path) -> tuple[list[dict], list[dict]]:
    segs = load_json(job / "segments.json", [])
    tr = load_json(job / "translations.json", {})
    text = [s for s in segs if str(s["id"]) not in tr]
    if not load_json(job / "job.json", {}).get("images", True):
        return text, []  # --no-images: older image segments of this job do not count
    isegs = load_json(job / "img_segments.json", [])
    itr = load_json(job / "img_translations.json", {})
    done_src = {s["src"] for s in isegs if str(s["id"]) in itr}
    img, seen = [], set()
    for s in isegs:  # one entry per distinct source text: build() shares it across repeats
        if str(s["id"]) not in itr and s["src"] not in done_src and s["src"] not in seen:
            seen.add(s["src"])
            img.append(s)
    return text, img


# --------------------------------------------------------------------------- commands
def cmd_check(_args):
    import importlib.metadata as md

    import fonts
    import image_translate as it

    def line(ok, what, detail=""):
        print(f"[{'OK' if ok else '--'}] {what}{(': ' + detail) if detail else ''}")

    line(True, "runtime", str(P.HOME))
    line(True, "python", sys.version.split()[0])
    for pkg in ("babeldoc", "pymupdf", "rapidocr"):
        try:
            line(True, pkg, md.version(pkg))
        except md.PackageNotFoundError:
            line(False, pkg, "missing - run `lt.py setup --force`")
    models = list((P.PROFILE / ".cache" / "babeldoc" / "models").glob("*.onnx"))
    line(bool(models), "BabelDOC layout model", str(P.PROFILE / ".cache" / "babeldoc"))
    for ok, what, detail in fonts.check():
        line(ok, what, detail)
    have = sorted({p.name for p in P.MODELS.rglob("*.onnx")}) if P.MODELS.exists() else []
    line(bool(have), "OCR models (more languages download on first use)", ", ".join(have) or "none yet")
    line(True, "jobs folder", str(P.JOBS))
    _ = it  # imported to prove the OCR module loads


def cmd_prepare(args):
    import pymupdf

    src = Path(args.pdf).resolve()
    if not src.exists():
        sys.exit(f"not found: {src}")
    job = Path(args.job).resolve() if args.job else default_job(src)
    job.mkdir(parents=True, exist_ok=True)
    with JobLock(job):
        _prepare(args, src, job)


def _prepare(args, src: Path, job: Path):
    import pymupdf

    pdf = job / "source.pdf"
    shutil.copy2(src, pdf)
    doc = pymupdf.open(pdf)
    if doc.needs_pass:
        sys.exit("the PDF is password-protected; remove the password first")
    profile = detect_profile(doc) if args.profile == "auto" else args.profile
    lang = detect_lang(doc) if args.lang_in == "auto" else args.lang_in
    if lang is None:
        sys.exit("cannot tell the source language (little or no text layer - a scan?): "
                 "pass --lang-in ko|en|ja|fr|de|ru|...")
    if lang == "zh" and args.lang_in == "auto":
        sys.exit("the document already looks Chinese (e.g. Traditional); pass --lang-in zh to translate it anyway")
    cfg = {
        "source": str(src), "name": src.stem, "pages_total": doc.page_count,
        "creator": (doc.metadata or {}).get("creator"), "producer": (doc.metadata or {}).get("producer"),
        "profile": profile, "options": PROFILES[profile], "lang_in": lang, "pages": args.pages,
        "images": not args.no_images,
        "carry_from": [str(Path(p).resolve()) for p in args.carry_from.split(",")] if args.carry_from else [],
        "prepared": datetime.now().isoformat(timespec="seconds"),
    }
    doc.close()
    save_json(job / "job.json", cfg, ascii_only=True)
    print(f"job: {job}\nprofile: {profile}  source language: {lang}  pages: {cfg['pages_total']}"
          f"{'  (only ' + args.pages + ')' if args.pages else ''}  images: {cfg['images']}")
    run_pass(job, cfg, "record", pdf)
    text, img = pending(job)
    print(f"\nto translate: {len(text)} text segments, {len(img)} image texts  ->  lt.py todo \"{job}\"")


def cmd_todo(args):
    job = need_job(args.job)
    text, img = pending(job)
    items = []
    if args.kind in ("all", "text"):
        items += [{"k": str(s["id"]), "src": s["src"], **({"page": s["page"]} if s.get("page") else {}),
                   **({"ph": s["ph"]} if s.get("ph") else {})} for s in text]
    if args.kind in ("all", "img"):
        items += [{"k": f"i{s['id']}", "src": s["src"], "page": s["page"],
                   "image": str(job / "img" / f"x{s['img']}.png")} for s in img]
    part = items[args.offset:args.offset + args.limit]
    head = (f"# pending: {len(text)} text, {len(img)} image; showing {args.offset + 1 if part else 0}"
            f"-{args.offset + len(part)} of {len(items)}")
    if args.out:  # UTF-8 file: safe even when the agent's shell decodes output with a legacy code page
        Path(args.out).write_text("\n".join([head] + [json.dumps(i, ensure_ascii=False) for i in part]) + "\n",
                                  "utf-8")
        print(f"{head} -> {Path(args.out).resolve()}")
        return
    print(head)
    for it in part:
        print(json.dumps(it, ensure_ascii=False))


def cmd_merge(args):
    job = need_job(args.job)
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig") if args.file == "-" else \
            Path(args.file).read_text("utf-8-sig")
        new = json.loads(raw)
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {args.file}: {e}")
    if not isinstance(new, dict):
        sys.exit('the file must hold one JSON object: {"12": "译文", "i7": "译文"}')
    segs = {str(s["id"]): s for s in load_json(job / "segments.json", [])}
    isegs = {str(s["id"]): s for s in load_json(job / "img_segments.json", [])}
    tr = load_json(job / "translations.json", {})
    itr = load_json(job / "img_translations.json", {})
    warn, n_t, n_i = [], 0, 0
    for k, v in new.items():
        k = str(k)
        if not isinstance(v, str):
            warn.append(f"{k}: translation is not a string")
        elif k.startswith("i"):
            if k[1:] not in isegs:
                warn.append(f"{k}: no such image segment")
                continue
            itr[k[1:]] = v
            n_i += 1
        else:
            if k not in segs:
                warn.append(f"{k}: no such text segment")
                continue
            want, got = sorted(PH_RE.findall(segs[k]["src"])), sorted(PH_RE.findall(v))
            if want != got:
                warn.append(f"{k}: placeholders differ - source {want}, translation {got}")
            tr[k] = v
            n_t += 1
    save_json(job / "translations.json", tr)
    save_json(job / "img_translations.json", itr)
    text, img = pending(job)
    print(f"merged {n_t} text + {n_i} image translations; still pending: {len(text)} text, {len(img)} image")
    for w in warn:
        print("WARNING", w)


def cmd_build(args):
    job = need_job(args.job)
    with JobLock(job):
        _build(args, job)


def _build(args, job: Path):
    cfg = load_json(job / "job.json", {})
    text, img = pending(job)
    if text or img:
        print(f"WARNING: {len(text)} text / {len(img)} image segments untranslated - they stay in the source language")
    pdf = job / "source.pdf"
    result = run_pass(job, cfg, "replay", pdf)
    if result is None or not getattr(result, "mono_pdf_path", None):
        sys.exit("BabelDOC produced no output")
    out = Path(args.out).resolve() if args.out else job / "output"
    out.mkdir(parents=True, exist_ok=True)
    name = cfg["name"]
    mono = safe_copy(Path(result.mono_pdf_path), out / f"{name}.中文.pdf")
    dual = None
    if getattr(result, "dual_pdf_path", None) and Path(result.dual_pdf_path).exists():
        dual = safe_copy(Path(result.dual_pdf_path), out / f"{name}.中外对照.pdf")
    missing = load_json(job / "missing.json", [])
    print(f"\n中文版: {mono}" + (f"\n对照版: {dual}" if dual else ""))
    if missing:
        print(f"WARNING: {len(missing)} paragraphs were left in the source language (see {job / 'missing.json'})")
    shots = []
    if args.compare != "none":
        shots = compare(pdf, mono, job / "review", args.compare)
        print(f"review images ({len(shots)}): {job / 'review'}")
    # machine-readable result (paths survive shells that mangle non-ASCII output)
    save_json(job / "result.json", {"mono": str(mono), "dual": str(dual) if dual else None,
                                    "review": [str(s) for s in shots], "untranslated": len(missing)})
    cfg["last_out"] = str(out)
    save_json(job / "job.json", cfg, ascii_only=True)


def compare(orig: Path, trans: Path, out_dir: Path, pages_arg: str, width: int = 1600) -> list[Path]:
    """Side-by-side PNGs (left original, right translation) for checking the result."""
    import pymupdf

    from image_translate import parse_pages

    out_dir.mkdir(parents=True, exist_ok=True)
    a, b = pymupdf.open(orig), pymupdf.open(trans)
    sel = None if pages_arg == "all" else parse_pages(pages_arg)
    pages = [p + 1 for p in range(a.page_count) if sel is None or p in sel]
    shots = []
    for p in pages:
        if not 1 <= p <= min(a.page_count, b.page_count):
            continue
        r = a[p - 1].rect
        gap = r.width * 0.02
        sheet = pymupdf.open()
        pg = sheet.new_page(width=r.width * 2 + gap, height=r.height)
        pg.draw_rect(pg.rect, color=None, fill=(0.6, 0.6, 0.6))
        left, right = pymupdf.Rect(0, 0, r.width, r.height), pymupdf.Rect(r.width + gap, 0, r.width * 2 + gap, r.height)
        pg.draw_rect(left, color=None, fill=(1, 1, 1))
        pg.draw_rect(right, color=None, fill=(1, 1, 1))
        pg.show_pdf_page(left, a, p - 1)
        pg.show_pdf_page(right, b, p - 1)
        zoom = width / pg.rect.width
        f = out_dir / f"p{p:03d}.png"
        pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(f)
        shots.append(f)
    return shots


def cmd_status(args):
    job = need_job(args.job)
    cfg = load_json(job / "job.json", {})
    text, img = pending(job)
    print(json.dumps({k: cfg.get(k) for k in ("source", "profile", "lang_in", "pages", "images")},
                     ensure_ascii=False))
    print(f"segments: {len(load_json(job / 'segments.json', []))} text, "
          f"{len(load_json(job / 'img_segments.json', []))} image; pending {len(text)} text, {len(img)} image")
    out = Path(cfg.get("last_out") or job / "output")
    for f in sorted(out.glob(f"{cfg.get('name', '*')}*.pdf")) if out.exists() else []:
        print("output:", f)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="layout-preserving PDF translation to Simplified Chinese")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--force", action="store_true")
    sub.add_parser("check")
    p = sub.add_parser("prepare")
    p.add_argument("pdf")
    p.add_argument("--job")
    p.add_argument("--lang-in", default="auto")
    p.add_argument("--profile", default="auto", choices=["auto", *PROFILES])
    p.add_argument("--pages")
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--carry-from")
    t = sub.add_parser("todo")
    t.add_argument("job")
    t.add_argument("--kind", default="all", choices=["all", "text", "img"])
    t.add_argument("--limit", type=int, default=60)
    t.add_argument("--offset", type=int, default=0)
    t.add_argument("--out", help="write the items to this UTF-8 file instead of the console")
    m = sub.add_parser("merge")
    m.add_argument("job")
    m.add_argument("file")
    b = sub.add_parser("build")
    b.add_argument("job")
    b.add_argument("--out")
    b.add_argument("--compare", default="all")
    st = sub.add_parser("status")
    st.add_argument("job")
    return ap


def main():
    args = parser().parse_args()
    if not in_runtime():
        if os.environ.get("LT_REEXEC") == "1":  # we were started as the runtime but are not in it
            sys.exit(f"internal error: expected to run inside {P.VENV}, got {sys.prefix}; "
                     f"try `lt.py setup --force`")
        import runtime_setup

        if args.cmd == "setup":
            runtime_setup.setup(force=args.force)
            return
        if not runtime_setup.is_ready():
            if args.cmd == "check":
                print(f"[--] runtime not installed yet ({P.HOME}); it is installed automatically on the "
                      f"first prepare, or now with `lt.py setup`")
                sys.exit(1)
            print("first run: installing the runtime (about 1.5 GB, needs internet, 5-15 min) ...", flush=True)
            runtime_setup.setup()
        # re-run this command inside the runtime (-s: ignore the user's site-packages)
        cmd, env = P.runtime_command(["-s", "-X", "utf8", Path(__file__).resolve(), *sys.argv[1:]])
        done = P.HOME / f"run-{os.getpid()}.done"  # the runtime folder is writable by design
        done.unlink(missing_ok=True)
        env["LT_REEXEC"] = "1"
        env["LT_DONE_FILE"] = str(done)
        rc = subprocess.call(cmd, env=env)
        finished = done.exists()
        done.unlink(missing_ok=True)
        if rc == 0 and not finished:
            sys.exit(
                "ERROR: the runtime process was stopped before it finished, without reporting an error.\n"
                "On Windows this is almost always security software (360, Tencent PC Manager, Huorong, "
                "Kaspersky ...) ending python.exe while it rewrites PDF files.  Add these folders to its "
                f"trusted list / exclusions, then run the same command again:\n  {P.HOME}\n  "
                f"{getattr(args, 'job', None) or P.JOBS}")
        sys.exit(rc)
    os.environ.update(P.runtime_env())  # also when someone runs the runtime's python directly
    if args.cmd == "setup":
        sys.exit("run `lt.py setup` with a normal Python, not the runtime's own interpreter")
    try:
        {"check": cmd_check, "prepare": cmd_prepare, "todo": cmd_todo, "merge": cmd_merge,
         "build": cmd_build, "status": cmd_status}[args.cmd](args)
    except SystemExit as e:
        if e.code in (None, 0):
            _mark_done()
        raise
    _mark_done()


def _mark_done():
    """Tell the launching process that the command ran to its end (see the check in main)."""
    marker = os.environ.pop("LT_DONE_FILE", "")
    if marker:
        try:
            Path(marker).write_text("ok", "ascii")
        except OSError:
            pass


if __name__ == "__main__":
    main()
