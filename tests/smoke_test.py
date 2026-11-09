#!/usr/bin/env python3
"""End-to-end smoke test: build a small PDF (text, list, a screenshot-like image with text),
run prepare -> todo -> merge -> build with a dummy "translation", and check the outputs.

    python tests/smoke_test.py            # uses / installs the runtime like a normal run

Needs the runtime (installed automatically on first use).  Nothing is uploaded.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LT = ROOT / "skills" / "pdf-layout-translate" / "scripts" / "lt.py"
OUT = ROOT / "tests" / "out"


def lt(*args: str) -> str:
    r = subprocess.run([sys.executable, str(LT), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    print(r.stdout[-3000:])
    if r.returncode:
        print(r.stderr[-3000:])
        raise SystemExit(f"lt.py {args[0]} failed ({r.returncode})")
    return r.stdout


def run_rt(*args: str, **kw):
    """Run the runtime's python with the runtime environment (the same way lt.py starts it)."""
    sys.path.insert(0, str(LT.parent))
    import lt_paths
    cmd, env = lt_paths.runtime_command(["-s", *args])
    return subprocess.run(cmd, env=env, **kw)


MAKE_PDF = r'''
import sys, pymupdf
out = sys.argv[1]
# a "screenshot": text rendered on a grey panel, rasterised to a PNG
shot = pymupdf.open(); sp = shot.new_page(width=360, height=120)
sp.draw_rect(sp.rect, color=None, fill=(0.93, 0.93, 0.93))
sp.insert_text((20, 45), "Please select the installation folder.", fontsize=14)
sp.insert_text((20, 85), "SELECT * FROM student;", fontname="cour", fontsize=13)
png = sp.get_pixmap(dpi=144).tobytes("png")
doc = pymupdf.open(); pg = doc.new_page(width=720, height=540)
pg.insert_text((50, 70), "Introduction to Databases", fontsize=28)
pg.insert_text((50, 120), "A database is an organized collection of data.", fontsize=16)
pg.insert_text((50, 150), "a. Install the server and open a terminal.", fontsize=16)
pg.insert_text((50, 175), "b. Run the query shown below.", fontsize=16)
pg.insert_image(pymupdf.Rect(50, 220, 590, 400), stream=png)
doc.set_metadata({"creator": "Google", "producer": "Skia/PDF"})
doc.save(out)
'''


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    # first run installs the runtime (5-15 min): show its progress instead of capturing it
    subprocess.run([sys.executable, str(LT), "setup"], check=True)
    pdf, job = OUT / "sample.pdf", OUT / "job"
    run_rt("-c", MAKE_PDF, str(pdf), check=True)

    lt("prepare", str(pdf), "--job", str(job), "--lang-in", "en")
    todo = [json.loads(line) for line in lt("todo", str(job), "--limit", "500").splitlines() if line.startswith("{")]
    assert any(t["k"].startswith("i") for t in todo), "no text found inside the image"
    fill = {}
    for t in todo:
        src = t["src"]
        if t["k"].startswith("i"):
            fill[t["k"]] = "请选择安装文件夹。" if "installation" in src else src  # keep the SQL line
        else:
            fill[t["k"]] = "【译】" + src  # dummy translation that keeps every placeholder
    (job / "fill.json").write_text(json.dumps(fill, ensure_ascii=False), "utf-8")
    lt("merge", str(job), str(job / "fill.json"))
    out = lt("build", str(job), "--out", str(OUT / "result"), "--compare", "1")

    mono = OUT / "result" / "sample.中文.pdf"
    dual = OUT / "result" / "sample.中外对照.pdf"
    assert mono.exists() and dual.exists(), out
    check = run_rt("-c", (
        "import sys, pymupdf\n"
        "m = pymupdf.open(sys.argv[1]); d = pymupdf.open(sys.argv[2]); s = pymupdf.open(sys.argv[3])\n"
        "assert m.page_count == s.page_count == 1 and d.page_count == 1, 'page count'\n"
        "assert d[0].rect.width > 1.9 * s[0].rect.width, 'dual is not side by side'\n"
        "assert '【译】' in m[0].get_text(), 'translation missing from the text layer'\n"
        "xi = [i[0] for i in m[0].get_images()]; xs = [i[0] for i in s[0].get_images()]\n"
        "assert m.extract_image(xi[0])['image'] != s.extract_image(xs[0])['image'], 'image not replaced'\n"
        "print('checks passed')"), str(mono), str(dual), str(pdf), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    print(check.stdout, check.stderr)
    if check.returncode:
        raise SystemExit("output checks failed")
    print(f"OK - outputs in {OUT / 'result'}, review image in {job / 'review'}")


if __name__ == "__main__":
    main()
