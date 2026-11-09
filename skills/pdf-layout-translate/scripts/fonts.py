"""Cross-platform font lookup, and the renamed math fonts used for formula-preserving rewrites.

- find(): look up font files by name in the system font folders (Windows / macOS / Linux) and in
  the fonts BabelDOC downloads (Noto, Source Han Sans/Serif), so every platform has a fallback.
- cjk(): Chinese font for text drawn into images (Source Han Sans CN from BabelDOC's cache).
- build: "LtMath-*" copies of Cambria (Windows) or STIX Two (system, else downloaded from the
  google/fonts repository, OFL-1.1).  BabelDOC treats fonts named '*Math' as formula fonts and keeps
  their glyphs verbatim, so italic letters and sub/superscripts of inline math survive translation.
  The copies are built on the user's machine and never redistributed.

usage: python fonts.py build | check
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from pathlib import Path

import lt_paths as P

_GF = "https://raw.githubusercontent.com/google/fonts/5bb28733cec313b63fbd415b574b5ae35c8aa8b8/ofl/"
STIX_URLS = {  # file -> (pinned google/fonts URL, sha256)
    "STIXTwoText-Regular.ttf": (_GF + "stixtwotext/STIXTwoText%5Bwght%5D.ttf",
                                "7962b8b7811e6a896c9a91a0bccbb5241047770eb24d4997c5cb5fe21d5c0df2"),
    "STIXTwoText-Italic.ttf": (_GF + "stixtwotext/STIXTwoText-Italic%5Bwght%5D.ttf",
                               "88c0e2e316eaff56eddc9e51e4850317e2a1e490bbf758b2dec4793aedba9c74"),
    "STIXTwoMath-Regular.ttf": (_GF + "stixtwomath/STIXTwoMath-Regular.ttf",
                                "562551b15b836e6e01d1b7350909baf3c8c8d83260c1190fbf4544333e6936de"),
}
MATH_FILES = {"regular": "LtMath-Regular.ttf", "italic": "LtMath-Italic.ttf", "symbol": "LtMathSym-Regular.ttf"}


def system_font_dirs() -> list[Path]:
    home = P.REAL_HOME
    if P.IS_WIN:
        dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
                Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local"))) / "Microsoft/Windows/Fonts"]
    elif sys.platform == "darwin":
        dirs = [Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"),
                Path("/Library/Fonts"), home / "Library/Fonts"]
    else:
        dirs = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".local/share/fonts", home / ".fonts"]
    return [d for d in dirs if d.is_dir()]


_INDEX: dict[str, str] | None = None


def _index() -> dict[str, str]:
    """lower-case file name -> path, over our fonts, BabelDOC's fonts and the system folders."""
    global _INDEX
    if _INDEX is None:
        _INDEX = {}
        for d in [P.FONTS, P.BABELDOC_FONTS, *system_font_dirs()]:
            if not d.is_dir():
                continue
            for root, _dirs, files in os.walk(d):
                for f in files:
                    if f.lower().endswith((".ttf", ".otf", ".ttc")):
                        _INDEX.setdefault(f.lower(), os.path.join(root, f))
    return _INDEX


def find(*names: str) -> str | None:
    """First existing font among file names (case-insensitive) or absolute paths."""
    idx = _index()
    for n in names:
        if os.path.isabs(n) and os.path.exists(n):
            return n
        hit = idx.get(n.lower())
        if hit:
            return hit
    return None


# style index: 0 regular, 1 italic, 2 bold, 3 bold italic
FAMILIES = {
    "sans": [("arial.ttf", "ariali.ttf", "arialbd.ttf", "arialbi.ttf"),
             ("Arial.ttf", "Arial Italic.ttf", "Arial Bold.ttf", "Arial Bold Italic.ttf"),
             ("LiberationSans-Regular.ttf", "LiberationSans-Italic.ttf", "LiberationSans-Bold.ttf",
              "LiberationSans-BoldItalic.ttf"),
             ("NotoSans-Regular.ttf", "NotoSans-Italic.ttf", "NotoSans-Bold.ttf", "NotoSans-BoldItalic.ttf")],
    "serif": [("times.ttf", "timesi.ttf", "timesbd.ttf", "timesbi.ttf"),
              ("Times New Roman.ttf", "Times New Roman Italic.ttf", "Times New Roman Bold.ttf",
               "Times New Roman Bold Italic.ttf"),
              ("LiberationSerif-Regular.ttf", "LiberationSerif-Italic.ttf", "LiberationSerif-Bold.ttf",
               "LiberationSerif-BoldItalic.ttf"),
              ("NotoSerif-Regular.ttf", "NotoSerif-Italic.ttf", "NotoSerif-Bold.ttf", "NotoSerif-BoldItalic.ttf")],
    "mono": [("consola.ttf", "consolai.ttf", "consolab.ttf", "consolaz.ttf"),
             ("cour.ttf", "couri.ttf", "courbd.ttf", "courbi.ttf"),
             ("Menlo.ttc",) * 4,
             ("DejaVuSansMono.ttf", "DejaVuSansMono-Oblique.ttf", "DejaVuSansMono-Bold.ttf",
              "DejaVuSansMono-BoldOblique.ttf"),
             ("LiberationMono-Regular.ttf", "LiberationMono-Italic.ttf", "LiberationMono-Bold.ttf",
              "LiberationMono-BoldItalic.ttf")],
    "gill": [("GIL_____.TTF", "GILI____.TTF", "GILB____.TTF", "GILBI___.TTF"), ("GillSans.ttc",) * 4],
    "calibri": [("calibri.ttf", "calibrii.ttf", "calibrib.ttf", "calibriz.ttf")],
    "korean": [("malgun.ttf", "malgun.ttf", "malgunbd.ttf", "malgunbd.ttf"), ("AppleSDGothicNeo.ttc",) * 4,
               ("NotoSansCJK-Regular.ttc", "NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc", "NotoSansCJK-Bold.ttc"),
               ("SourceHanSansKR-Regular.ttf", "SourceHanSansKR-Regular.ttf", "SourceHanSansKR-Bold.ttf",
                "SourceHanSansKR-Bold.ttf")],
}
FALLBACKS = ["cambria.ttc", "seguisym.ttf", "Apple Symbols.ttf", "DejaVuSans.ttf", "SourceHanSansKR-Regular.ttf",
             "SourceHanSansCN-Regular.ttf", "GoNotoKurrent-Regular.ttf", "NotoSans-Regular.ttf"]


def family_chain(family: str, style: int) -> list[str]:
    """Font files to try for one span: the family (any platform's version), then broad fallbacks."""
    out = []
    for variants in FAMILIES.get(family, []) + ([] if family == "sans" else FAMILIES["sans"]):
        f = find(variants[style]) or find(variants[0])
        if f and f not in out:
            out.append(f)
            break
    for name in FALLBACKS:
        f = find(name)
        if f and f not in out:
            out.append(f)
    return out


def math_chain(italic: bool) -> list[str]:
    out = [str(P.FONTS / MATH_FILES["italic" if italic else "regular"]), str(P.FONTS / MATH_FILES["symbol"])]
    out = [f for f in out if os.path.exists(f)]
    return out + [f for f in family_chain("serif", 1 if italic else 0) if f not in out]


def cjk(bold: bool = False) -> str:
    """Simplified-Chinese font for text drawn into images."""
    f = find(*(["SourceHanSansCN-Bold.ttf", "msyhbd.ttc"] if bold else []),
             "SourceHanSansCN-Regular.ttf", "msyh.ttc", "PingFang.ttc", "NotoSansCJK-Regular.ttc",
             "NotoSansCJKsc-Regular.otf", "wqy-microhei.ttc")
    if not f:
        raise FileNotFoundError("no Chinese font found - run `lt.py setup` (BabelDOC downloads Source Han Sans)")
    return f


# --------------------------------------------------------------------------- math fonts
def _math_sources() -> dict[str, tuple[str, int | None]]:
    """regular / italic / symbol -> (font file, index in a .ttc)."""
    cam, cami = find("cambria.ttc"), find("cambriai.ttf")
    if cam and cami:  # Windows (and Macs with Office)
        return {"regular": (cam, 0), "italic": (cami, None), "symbol": (cam, 1)}
    src = {}
    for key, name in (("regular", "STIXTwoText-Regular.ttf"), ("italic", "STIXTwoText-Italic.ttf"),
                      ("symbol", "STIXTwoMath-Regular.ttf")):
        f = find(name, name.replace(".ttf", ".otf"))
        if not f:
            dst = P.FONTS / "src" / name
            if not dst.exists():
                url, sha = STIX_URLS[name]
                print(f"[fonts] downloading {name} (STIX Two, OFL-1.1)", flush=True)
                data = urllib.request.urlopen(url, timeout=120).read()
                if hashlib.sha256(data).hexdigest() != sha:
                    raise RuntimeError(f"checksum mismatch for {name}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_suffix(".part")
                tmp.write_bytes(data)
                os.replace(tmp, dst)
            f = str(dst)
        src[key] = (f, None)
    return src


def build_math() -> dict[str, str]:
    from fontTools.ttLib import TTFont

    P.FONTS.mkdir(parents=True, exist_ok=True)
    done = {}
    for key, (path, idx) in _math_sources().items():
        out = P.FONTS / MATH_FILES[key]
        family = "LtMathSym" if key == "symbol" else "LtMath"
        style = "Italic" if key == "italic" else "Regular"
        font = TTFont(path, fontNumber=idx) if idx is not None else TTFont(path)
        if "fvar" in font:  # variable font (STIX Two Text): freeze the default weight
            from fontTools.varLib import instancer
            font = instancer.instantiateVariableFont(font, {"wght": 400})
        ps = f"{family}-{style}"
        names = {1: family, 2: style, 3: ps, 4: f"{family} {style}", 6: ps, 16: family, 17: style}
        for rec in list(font["name"].names):
            if rec.nameID in names:
                font["name"].setName(names[rec.nameID], rec.nameID, rec.platformID, rec.platEncID, rec.langID)
        font.save(out)
        done[key] = str(out)
        print(f"[fonts] {out.name} <- {Path(path).name}", flush=True)
    return done


def check() -> list[tuple[bool, str, str]]:
    rows = []
    try:
        rows.append((True, "Chinese font for images", cjk()))
    except FileNotFoundError as e:
        rows.append((False, "Chinese font for images", str(e)))
    ok = all((P.FONTS / f).exists() for f in MATH_FILES.values())
    rows.append((ok, "math fonts (inline formulas)", str(P.FONTS) if ok else "missing - run `lt.py setup`"))
    return rows


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build_math()
    else:
        for ok, what, detail in check():
            print(f"[{'OK' if ok else '--'}] {what}: {detail}")
