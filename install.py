#!/usr/bin/env python3
"""Install the pdf-layout-translate skill into the skill folders of local coding agents.

    python install.py                 # every agent found on this machine
    python install.py --agent claude,codex
    python install.py --dest ~/my-agent/skills      # only there (no auto-detection)
    python install.py --setup         # also install the runtime now (~1.5 GB) instead of on first use
    python install.py --list          # show where each agent would get it

Copies skills/pdf-layout-translate into <agent skills dir>/pdf-layout-translate.  An older copy
of this skill is replaced (its config.local.json is kept); a link (symlink / Windows junction,
e.g. a shared skills library) is left alone unless --replace-links is given; anything else with
that name is never touched.  Standard library only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NAME = "pdf-layout-translate"
SRC = Path(__file__).resolve().parent / "skills" / NAME
HOME = Path.home()


def _env_dir(var: str, default: Path) -> Path:
    v = os.environ.get(var, "").strip()
    return Path(v).expanduser() if v else default


CLAUDE = _env_dir("CLAUDE_CONFIG_DIR", HOME / ".claude")
CODEX = _env_dir("CODEX_HOME", HOME / ".codex")
XDG_CONFIG = _env_dir("XDG_CONFIG_HOME", HOME / ".config")

# agent -> (folder that shows the agent is installed, its personal skills folder)
AGENTS = {
    "claude": (CLAUDE, CLAUDE / "skills"),
    "codex": (CODEX, CODEX / "skills"),
    "gemini": (HOME / ".gemini", HOME / ".gemini" / "skills"),
    "antigravity": (HOME / ".gemini" / "antigravity", HOME / ".gemini" / "antigravity" / "skills"),
    "opencode": (XDG_CONFIG / "opencode", XDG_CONFIG / "opencode" / "skills"),
    "agents": (HOME / ".agents", HOME / ".agents" / "skills"),  # shared Agent Skills folder
}
# auto-detection skips folders another detected agent already reads (no duplicate skill listings):
# Codex also reads ~/.agents/skills; OpenCode also reads ~/.claude/skills
SKIP_IF = {"agents": "codex", "opencode": "claude"}


def _is_link(p: Path) -> bool:
    if p.is_symlink():
        return True
    try:  # Windows junction (Path.is_junction exists only on Python 3.12+)
        return bool(os.lstat(p).st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False


def _ours(p: Path) -> bool:
    md = p / "SKILL.md"
    try:
        return md.exists() and f"name: {NAME}" in md.read_text("utf-8", errors="replace")
    except OSError:
        return False


def install_to(dest_root: Path, replace_links: bool) -> Path | None:
    dest = dest_root / NAME
    src_real = SRC.resolve()
    try:
        dest_real = dest.resolve()
    except OSError:
        dest_real = dest
    if dest_real == src_real or src_real in dest_real.parents or dest_real in src_real.parents:
        print(f"skip {dest}: that is this repository itself")
        return None
    if _is_link(dest):
        if not replace_links:
            print(f"skip {dest}: it is a link to {os.path.realpath(dest)} (managed elsewhere; "
                  f"use --replace-links to replace the link with a copy)")
            return None
        os.rmdir(dest) if os.name == "nt" and dest.is_dir() else dest.unlink()  # the link only
    elif dest.exists() and not _ours(dest):
        print(f"skip {dest}: a different skill/folder has that name")
        return None
    dest_root.mkdir(parents=True, exist_ok=True)
    # copy next to the target first, then swap: a failed copy never leaves nothing behind
    tmp = Path(tempfile.mkdtemp(prefix=f".{NAME}-", dir=dest_root))
    new = tmp / NAME
    shutil.copytree(SRC, new, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "config.local.json"))
    keep = dest / "config.local.json"
    if keep.exists():
        shutil.copy2(keep, new / "config.local.json")
    old = None
    if dest.exists():
        old = tmp / "old"
        os.replace(dest, old)
    os.replace(new, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", help="comma-separated: " + ",".join(AGENTS))
    ap.add_argument("--dest", action="append", default=[], help="extra skills folder (repeatable)")
    ap.add_argument("--setup", action="store_true", help="install the runtime right away")
    ap.add_argument("--replace-links", action="store_true",
                    help="replace a symlink/junction named pdf-layout-translate with a copy")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for a, (marker, skills) in AGENTS.items():
            print(f"{a:12s} {'found' if marker.is_dir() else '-':6s} {skills / NAME}")
        return
    if args.agent:
        chosen = [a.strip() for a in args.agent.split(",") if a.strip()]
        unknown = [a for a in chosen if a not in AGENTS]
        if unknown:
            sys.exit(f"unknown agent(s): {unknown}; known: {list(AGENTS)} (or use --dest)")
    elif args.dest:
        chosen = []  # explicit folders only: no auto-detection
    else:
        found = [a for a, (marker, _) in AGENTS.items() if marker.is_dir()]
        chosen = [a for a in found if SKIP_IF.get(a) not in found]
    targets, seen = [], set()
    for t in [AGENTS[a][1] for a in chosen] + [Path(d).expanduser() for d in args.dest]:
        key = os.path.normcase(os.path.realpath(t))
        if key not in seen:  # e.g. two agents' folders linked to one place
            seen.add(key)
            targets.append(t)
    if not targets:
        sys.exit("no agent folder found; use --agent <name> or --dest <skills folder>")
    installed = [d for d in (install_to(t, args.replace_links) for t in targets) if d]
    for d in installed:
        print(f"installed: {d}")
    if not installed:
        sys.exit("nothing installed")
    print("\nStart a new agent session so it picks the skill up.")
    lt = installed[0] / "scripts" / "lt.py"
    if args.setup:
        subprocess.run([sys.executable, str(lt), "setup"], check=True)
    else:
        print(f"The runtime (~1.5 GB) is installed automatically on first use, or now with:\n  python \"{lt}\" setup")


if __name__ == "__main__":
    main()
